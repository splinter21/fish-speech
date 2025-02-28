import itertools
from typing import Any, Callable

import lightning as L
import torch
import torch.nn.functional as F
import wandb
from lightning.pytorch.loggers import TensorBoardLogger, WandbLogger
from matplotlib import pyplot as plt
from torch import nn
from vector_quantize_pytorch import VectorQuantize

from fish_speech.models.vqgan.losses import (
    discriminator_loss,
    feature_loss,
    generator_loss,
    kl_loss,
)
from fish_speech.models.vqgan.modules.decoder import Generator
from fish_speech.models.vqgan.modules.discriminator import EnsembleDiscriminator
from fish_speech.models.vqgan.modules.encoders import (
    ConvDownSampler,
    SpeakerEncoder,
    TextEncoder,
    VQEncoder,
)
from fish_speech.models.vqgan.utils import (
    plot_mel,
    rand_slice_segments,
    sequence_mask,
    slice_segments,
)


class VQGAN(L.LightningModule):
    def __init__(
        self,
        optimizer: Callable,
        lr_scheduler: Callable,
        downsample: ConvDownSampler,
        vq_encoder: VQEncoder,
        mel_encoder: TextEncoder,
        decoder: TextEncoder,
        generator: Generator,
        discriminator: EnsembleDiscriminator,
        mel_transform: nn.Module,
        segment_size: int = 20480,
        hop_length: int = 640,
        sample_rate: int = 32000,
        freeze_hifigan: bool = False,
        freeze_vq: bool = False,
        speaker_encoder: SpeakerEncoder = None,
    ):
        super().__init__()

        # Model parameters
        self.optimizer_builder = optimizer
        self.lr_scheduler_builder = lr_scheduler

        # Generator and discriminators
        self.downsample = downsample
        self.vq_encoder = vq_encoder
        self.mel_encoder = mel_encoder
        self.speaker_encoder = speaker_encoder
        self.decoder = decoder
        self.generator = generator
        self.discriminator = discriminator
        self.mel_transform = mel_transform

        # Crop length for saving memory
        self.segment_size = segment_size
        self.hop_length = hop_length
        self.sampling_rate = sample_rate
        self.freeze_hifigan = freeze_hifigan

        # Disable automatic optimization
        self.automatic_optimization = False

        # Stage 1: Train the VQ only
        if self.freeze_hifigan:
            for p in self.discriminator.parameters():
                p.requires_grad = False

            for p in self.generator.parameters():
                p.requires_grad = False

        # Stage 2: Train the HifiGAN + Decoder + Generator
        if freeze_vq:
            for p in self.vq_encoder.parameters():
                p.requires_grad = False

            for p in self.mel_encoder.parameters():
                p.requires_grad = False

            for p in self.downsample.parameters():
                p.requires_grad = False

    def configure_optimizers(self):
        # Need two optimizers and two schedulers
        optimizer_generator = self.optimizer_builder(
            itertools.chain(
                self.downsample.parameters(),
                self.vq_encoder.parameters(),
                self.mel_encoder.parameters(),
                self.decoder.parameters(),
                self.generator.parameters(),
            )
        )
        optimizer_discriminator = self.optimizer_builder(
            self.discriminator.parameters()
        )

        lr_scheduler_generator = self.lr_scheduler_builder(optimizer_generator)
        lr_scheduler_discriminator = self.lr_scheduler_builder(optimizer_discriminator)

        return (
            {
                "optimizer": optimizer_generator,
                "lr_scheduler": {
                    "scheduler": lr_scheduler_generator,
                    "interval": "step",
                    "name": "optimizer/generator",
                },
            },
            {
                "optimizer": optimizer_discriminator,
                "lr_scheduler": {
                    "scheduler": lr_scheduler_discriminator,
                    "interval": "step",
                    "name": "optimizer/discriminator",
                },
            },
        )

    def training_step(self, batch, batch_idx):
        optim_g, optim_d = self.optimizers()

        audios, audio_lengths = batch["audios"], batch["audio_lengths"]

        audios = audios.float()
        audios = audios[:, None, :]

        with torch.no_grad():
            features = gt_mels = self.mel_transform(
                audios, sample_rate=self.sampling_rate
            )

        if self.downsample is not None:
            features = self.downsample(features)

        mel_lengths = audio_lengths // self.hop_length
        feature_lengths = (
            audio_lengths
            / self.hop_length
            / (self.downsample.total_strides if self.downsample is not None else 1)
        ).long()

        feature_masks = torch.unsqueeze(
            sequence_mask(feature_lengths, features.shape[2]), 1
        ).to(gt_mels.dtype)
        mel_masks = torch.unsqueeze(sequence_mask(mel_lengths, gt_mels.shape[2]), 1).to(
            gt_mels.dtype
        )

        # vq_features is 50 hz, need to convert to true mel size
        text_features = self.mel_encoder(features, feature_masks)
        text_features, _, loss_vq = self.vq_encoder(text_features, feature_masks)
        text_features = F.interpolate(
            text_features, size=gt_mels.shape[2], mode="nearest"
        )

        # Sample mels
        speaker_features = (
            self.speaker_encoder(gt_mels, mel_masks)
            if self.speaker_encoder is not None
            else None
        )
        decoded_mels = self.decoder(text_features, mel_masks, g=speaker_features)
        fake_audios = self.generator(decoded_mels)

        y_hat_mels = self.mel_transform(fake_audios.squeeze(1))

        y, ids_slice = rand_slice_segments(audios, audio_lengths, self.segment_size)
        y_hat = slice_segments(fake_audios, ids_slice, self.segment_size)

        assert y.shape == y_hat.shape, f"{y.shape} != {y_hat.shape}"

        # Since we don't want to update the discriminator, we skip the backward pass
        if self.freeze_hifigan is False:
            # Discriminator
            y_d_hat_r, y_d_hat_g, _, _ = self.discriminator(y, y_hat.detach())

            with torch.autocast(device_type=audios.device.type, enabled=False):
                loss_disc_all, _, _ = discriminator_loss(y_d_hat_r, y_d_hat_g)

            self.log(
                "train/discriminator/loss",
                loss_disc_all,
                on_step=True,
                on_epoch=False,
                prog_bar=True,
                logger=True,
                sync_dist=True,
            )

            optim_d.zero_grad()
            self.manual_backward(loss_disc_all)
            self.clip_gradients(
                optim_d, gradient_clip_val=1.0, gradient_clip_algorithm="norm"
            )
            optim_d.step()

        y_d_hat_r, y_d_hat_g, fmap_r, fmap_g = self.discriminator(y, y_hat)

        with torch.autocast(device_type=audios.device.type, enabled=False):
            loss_decoded_mel = F.l1_loss(gt_mels * mel_masks, decoded_mels * mel_masks)
            loss_mel = F.l1_loss(gt_mels * mel_masks, y_hat_mels * mel_masks)
            loss_adv, _ = generator_loss(y_d_hat_g)
            loss_fm = feature_loss(fmap_r, fmap_g)

            if self.freeze_hifigan is True:
                loss_gen_all = loss_decoded_mel + loss_vq
            else:
                loss_gen_all = loss_mel * 45 + loss_vq * 45 + loss_fm + loss_adv

        self.log(
            "train/generator/loss",
            loss_gen_all,
            on_step=True,
            on_epoch=False,
            prog_bar=True,
            logger=True,
            sync_dist=True,
        )
        self.log(
            "train/generator/loss_decoded_mel",
            loss_decoded_mel,
            on_step=True,
            on_epoch=False,
            prog_bar=False,
            logger=True,
            sync_dist=True,
        )
        self.log(
            "train/generator/loss_mel",
            loss_mel,
            on_step=True,
            on_epoch=False,
            prog_bar=False,
            logger=True,
            sync_dist=True,
        )
        self.log(
            "train/generator/loss_fm",
            loss_fm,
            on_step=True,
            on_epoch=False,
            prog_bar=False,
            logger=True,
            sync_dist=True,
        )
        self.log(
            "train/generator/loss_adv",
            loss_adv,
            on_step=True,
            on_epoch=False,
            prog_bar=False,
            logger=True,
            sync_dist=True,
        )
        self.log(
            "train/generator/loss_vq",
            loss_vq,
            on_step=True,
            on_epoch=False,
            prog_bar=False,
            logger=True,
            sync_dist=True,
        )

        optim_g.zero_grad()
        self.manual_backward(loss_gen_all)
        self.clip_gradients(
            optim_g, gradient_clip_val=1.0, gradient_clip_algorithm="norm"
        )
        optim_g.step()

        # Manual LR Scheduler
        scheduler_g, scheduler_d = self.lr_schedulers()
        scheduler_g.step()
        scheduler_d.step()

    def validation_step(self, batch: Any, batch_idx: int):
        audios, audio_lengths = batch["audios"], batch["audio_lengths"]

        audios = audios.float()
        audios = audios[:, None, :]

        features = gt_mels = self.mel_transform(audios, sample_rate=self.sampling_rate)

        if self.downsample is not None:
            features = self.downsample(features)

        mel_lengths = audio_lengths // self.hop_length
        feature_lengths = (
            audio_lengths
            / self.hop_length
            / (self.downsample.total_strides if self.downsample is not None else 1)
        ).long()

        feature_masks = torch.unsqueeze(
            sequence_mask(feature_lengths, features.shape[2]), 1
        ).to(gt_mels.dtype)
        mel_masks = torch.unsqueeze(sequence_mask(mel_lengths, gt_mels.shape[2]), 1).to(
            gt_mels.dtype
        )

        # vq_features is 50 hz, need to convert to true mel size
        text_features = self.mel_encoder(features, feature_masks)
        text_features, _, _ = self.vq_encoder(text_features, feature_masks)
        text_features = F.interpolate(
            text_features, size=gt_mels.shape[2], mode="nearest"
        )

        # Sample mels
        speaker_features = (
            self.speaker_encoder(gt_mels, mel_masks)
            if self.speaker_encoder is not None
            else None
        )
        decoded_mels = self.decoder(text_features, mel_masks, g=speaker_features)
        fake_audios = self.generator(decoded_mels)

        fake_mels = self.mel_transform(fake_audios.squeeze(1))

        min_mel_length = min(
            decoded_mels.shape[-1], gt_mels.shape[-1], fake_mels.shape[-1]
        )
        decoded_mels = decoded_mels[:, :, :min_mel_length]
        gt_mels = gt_mels[:, :, :min_mel_length]
        fake_mels = fake_mels[:, :, :min_mel_length]

        mel_loss = F.l1_loss(gt_mels * mel_masks, fake_mels * mel_masks)
        self.log(
            "val/mel_loss",
            mel_loss,
            on_step=False,
            on_epoch=True,
            prog_bar=True,
            logger=True,
            sync_dist=True,
        )

        for idx, (
            mel,
            gen_mel,
            decode_mel,
            audio,
            gen_audio,
            audio_len,
        ) in enumerate(
            zip(
                gt_mels,
                fake_mels,
                decoded_mels,
                audios.detach().float(),
                fake_audios.detach().float(),
                audio_lengths,
            )
        ):
            mel_len = audio_len // self.hop_length

            image_mels = plot_mel(
                [
                    gen_mel[:, :mel_len],
                    decode_mel[:, :mel_len],
                    mel[:, :mel_len],
                ],
                [
                    "Generated",
                    "Decoded",
                    "Ground-Truth",
                ],
            )

            if isinstance(self.logger, WandbLogger):
                self.logger.experiment.log(
                    {
                        "reconstruction_mel": wandb.Image(image_mels, caption="mels"),
                        "wavs": [
                            wandb.Audio(
                                audio[0, :audio_len],
                                sample_rate=self.sampling_rate,
                                caption="gt",
                            ),
                            wandb.Audio(
                                gen_audio[0, :audio_len],
                                sample_rate=self.sampling_rate,
                                caption="prediction",
                            ),
                        ],
                    },
                )

            if isinstance(self.logger, TensorBoardLogger):
                self.logger.experiment.add_figure(
                    f"sample-{idx}/mels",
                    image_mels,
                    global_step=self.global_step,
                )
                self.logger.experiment.add_audio(
                    f"sample-{idx}/wavs/gt",
                    audio[0, :audio_len],
                    self.global_step,
                    sample_rate=self.sampling_rate,
                )
                self.logger.experiment.add_audio(
                    f"sample-{idx}/wavs/prediction",
                    gen_audio[0, :audio_len],
                    self.global_step,
                    sample_rate=self.sampling_rate,
                )

            plt.close(image_mels)


class VQNaive(L.LightningModule):
    def __init__(
        self,
        optimizer: Callable,
        lr_scheduler: Callable,
        downsample: ConvDownSampler,
        vq_encoder: VQEncoder,
        speaker_encoder: SpeakerEncoder,
        mel_encoder: TextEncoder,
        decoder: TextEncoder,
        mel_transform: nn.Module,
        hop_length: int = 640,
        sample_rate: int = 32000,
        vocoder: Generator = None,
    ):
        super().__init__()

        # Model parameters
        self.optimizer_builder = optimizer
        self.lr_scheduler_builder = lr_scheduler

        # Generator and discriminators
        self.downsample = downsample
        self.vq_encoder = vq_encoder
        self.speaker_encoder = speaker_encoder
        self.mel_encoder = mel_encoder
        self.decoder = decoder
        self.mel_transform = mel_transform

        # Crop length for saving memory
        self.hop_length = hop_length
        self.sampling_rate = sample_rate

        # Vocoder
        self.vocoder = vocoder

        for p in self.vocoder.parameters():
            p.requires_grad = False

    def configure_optimizers(self):
        optimizer = self.optimizer_builder(self.parameters())
        lr_scheduler = self.lr_scheduler_builder(optimizer)

        return {
            "optimizer": optimizer,
            "lr_scheduler": {
                "scheduler": lr_scheduler,
                "interval": "step",
            },
        }

    def vq_encode(self, audios, audio_lengths):
        with torch.no_grad():
            features = gt_mels = self.mel_transform(
                audios, sample_rate=self.sampling_rate
            )

        if self.downsample is not None:
            features = self.downsample(features)

        mel_lengths = audio_lengths // self.hop_length
        feature_lengths = (
            audio_lengths
            / self.hop_length
            / (self.downsample.total_strides if self.downsample is not None else 1)
        ).long()

        feature_masks = torch.unsqueeze(
            sequence_mask(feature_lengths, features.shape[2]), 1
        ).to(gt_mels.dtype)
        mel_masks = torch.unsqueeze(sequence_mask(mel_lengths, gt_mels.shape[2]), 1).to(
            gt_mels.dtype
        )

        # vq_features is 50 hz, need to convert to true mel size
        text_features = self.mel_encoder(features, feature_masks)
        text_features, indices, loss_vq = self.vq_encoder(text_features, feature_masks)

        return mel_masks, gt_mels, text_features, indices, loss_vq

    def vq_decode(self, text_features, speaker_features, gt_mels, mel_masks):
        text_features = F.interpolate(
            text_features, size=gt_mels.shape[2], mode="nearest"
        )

        decoded_mels = self.decoder(text_features, mel_masks, g=speaker_features)

        return decoded_mels

    def training_step(self, batch, batch_idx):
        audios, audio_lengths = batch["audios"], batch["audio_lengths"]

        audios = audios.float()
        audios = audios[:, None, :]

        mel_masks, gt_mels, text_features, indices, loss_vq = self.vq_encode(
            audios, audio_lengths
        )
        speaker_features = self.speaker_encoder(gt_mels, mel_masks)
        decoded_mels = self.vq_decode(
            text_features, speaker_features, gt_mels, mel_masks
        )
        loss_mel = F.l1_loss(gt_mels * mel_masks, decoded_mels * mel_masks)
        loss = loss_mel + loss_vq

        self.log(
            "train/generator/loss",
            loss,
            on_step=True,
            on_epoch=False,
            prog_bar=True,
            logger=True,
            sync_dist=True,
        )
        self.log(
            "train/loss_mel",
            loss_mel,
            on_step=True,
            on_epoch=False,
            prog_bar=False,
            logger=True,
            sync_dist=True,
        )
        self.log(
            "train/generator/loss_vq",
            loss_vq,
            on_step=True,
            on_epoch=False,
            prog_bar=False,
            logger=True,
            sync_dist=True,
        )

        return loss

    def validation_step(self, batch: Any, batch_idx: int):
        audios, audio_lengths = batch["audios"], batch["audio_lengths"]

        audios = audios.float()
        audios = audios[:, None, :]

        mel_masks, gt_mels, text_features, indices, loss_vq = self.vq_encode(
            audios, audio_lengths
        )
        speaker_features = self.speaker_encoder(gt_mels, mel_masks)
        decoded_mels = self.vq_decode(
            text_features, speaker_features, gt_mels, mel_masks
        )
        fake_audios = self.vocoder(decoded_mels)

        mel_loss = F.l1_loss(gt_mels * mel_masks, decoded_mels * mel_masks)
        self.log(
            "val/mel_loss",
            mel_loss,
            on_step=False,
            on_epoch=True,
            prog_bar=True,
            logger=True,
            sync_dist=True,
        )

        for idx, (
            mel,
            decoded_mel,
            audio,
            gen_audio,
            audio_len,
        ) in enumerate(
            zip(
                gt_mels,
                decoded_mels,
                audios.detach().float(),
                fake_audios.detach().float(),
                audio_lengths,
            )
        ):
            mel_len = audio_len // self.hop_length

            image_mels = plot_mel(
                [
                    decoded_mel[:, :mel_len],
                    mel[:, :mel_len],
                ],
                [
                    "Generated",
                    "Ground-Truth",
                ],
            )

            if isinstance(self.logger, WandbLogger):
                self.logger.experiment.log(
                    {
                        "reconstruction_mel": wandb.Image(image_mels, caption="mels"),
                        "wavs": [
                            wandb.Audio(
                                audio[0, :audio_len],
                                sample_rate=self.sampling_rate,
                                caption="gt",
                            ),
                            wandb.Audio(
                                gen_audio[0, :audio_len],
                                sample_rate=self.sampling_rate,
                                caption="prediction",
                            ),
                        ],
                    },
                )

            if isinstance(self.logger, TensorBoardLogger):
                self.logger.experiment.add_figure(
                    f"sample-{idx}/mels",
                    image_mels,
                    global_step=self.global_step,
                )
                self.logger.experiment.add_audio(
                    f"sample-{idx}/wavs/gt",
                    audio[0, :audio_len],
                    self.global_step,
                    sample_rate=self.sampling_rate,
                )
                self.logger.experiment.add_audio(
                    f"sample-{idx}/wavs/prediction",
                    gen_audio[0, :audio_len],
                    self.global_step,
                    sample_rate=self.sampling_rate,
                )

            plt.close(image_mels)

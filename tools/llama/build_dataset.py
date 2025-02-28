import re
from collections import defaultdict
from multiprocessing import Pool

import numpy as np
from loguru import logger
from tqdm import tqdm

from fish_speech.datasets.protos.text_data_pb2 import Semantics, Sentence, TextData
from fish_speech.datasets.protos.text_data_stream import pack_pb_stream
from fish_speech.text import g2p
from fish_speech.utils.file import AUDIO_EXTENSIONS, list_files

# Define datasets
DATASETS = [
    # (root, name, languages, extension, group parent level)
    ("data/StarRail/Chinese", "StarRail", ["ZH", "EN"], ".lab", 1),
    ("data/StarRail/English", "StarRail", ["EN"], ".lab", 1),
    ("data/StarRail/Japanese", "StarRail", ["JP", "EN"], ".lab", 1),
    ("data/Genshin/Chinese", "Genshin", ["ZH", "EN"], ".lab", 1),
    ("data/Genshin/English", "Genshin", ["EN"], ".lab", 1),
    ("data/Genshin/Japanese", "Genshin", ["JP", "EN"], ".lab", 1),
    ("data/LibriTTS_R", "LibriTTS_R", ["EN"], ".normalized.txt", 2),
    ("data/WenetSpeech", "WenetSpeech", ["ZH", "EN"], ".txt", 1),
]


def task_generator():
    for root, source, languages, extension, parent_level in DATASETS:
        # Load the files
        files = list_files(root, AUDIO_EXTENSIONS, recursive=True)

        grouped_files = defaultdict(list)
        for file in files:
            if parent_level == 1:
                p = file.parent.name
            elif parent_level == 2:
                p = file.parent.parent.name
            else:
                raise ValueError(f"Invalid parent level {parent_level}")

            grouped_files[p].append(file)

        logger.info(f"Found {len(grouped_files)} groups in {root}")
        for name, subset in grouped_files.items():
            yield name, subset, source, languages, extension


def run_task(task):
    name, subset, source, languages, extension = task

    # Parse the files
    sentences = []
    for file in subset:
        np_file = file.with_suffix(".npy")
        txt_file = file.with_suffix(extension)
        if np_file.exists() is False or txt_file.exists() is False:
            continue

        with open(txt_file, "r") as f:
            text = f.read().strip()

        # Simple cleaning: replace { xxx } and < xxx > with space
        text = re.sub(r"\{.*?\}", " ", text)
        text = re.sub(r"<.*?>", " ", text)
        text = re.sub(r"\s+", " ", text)

        try:
            phones = [v for _, v in g2p(text, order=languages)]
            semantics = np.load(np_file)
        except Exception as e:
            logger.error(f"Failed to parse {file}: {e}")
            continue

        if isinstance(semantics, np.ndarray):
            semantics = semantics.tolist()

        sentences.append(
            Sentence(
                text=text,
                phones=phones,
                semantics=[Semantics(values=s) for s in semantics],
            )
        )

    # Pack the sentences
    return pack_pb_stream(
        TextData(
            source=source,
            name=name,
            languages=languages,
            sentences=sentences,
        )
    )


def main():
    dataset_fp = open("data/quantized-dataset-1208.protos", "wb")
    with Pool(16) as p:
        for result in tqdm(p.imap_unordered(run_task, task_generator())):
            dataset_fp.write(result)

    dataset_fp.close()


if __name__ == "__main__":
    main()

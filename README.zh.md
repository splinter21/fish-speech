# Fish Speech

此代码库根据 BSD-3-Clause 许可证发布，所有模型根据 CC-BY-NC-SA-4.0 许可证发布。请参阅 [LICENSE](LICENSE) 了解更多细节。

## 免责声明
我们不对代码库的任何非法使用承担任何责任。请参阅您当地关于DMCA和其他相关法律的法律。

## 要求
- GPU内存：2GB（用于推理），24GB（用于微调）
- 系统：Linux（全部功能），Windows（仅推理，不支持flash-attn，不支持torch.compile）

因此，我们强烈建议Windows用户使用WSL2或docker来运行代码库。

## 设置
```bash
# 基本环境设置
conda create -n fish-speech python=3.10
conda activate fish-speech
conda install pytorch torchvision torchaudio pytorch-cuda=12.1 -c pytorch -c nvidia

# 安装 flash-attn（适用于linux）
pip3 install ninja && MAX_JOBS=4 pip3 install flash-attn --no-build-isolation

# 安装 fish-speech
pip3 install -e .
```

## 推理（CLI）

从我们的 huggingface 仓库下载所需的 `vqgan` 和 `text2semantic` 模型。
    
```bash
wget https://huggingface.co/fishaudio/speech-lm-v1/raw/main/vqgan-v1.pth -O checkpoints/vqgan-v1.pth
wget https://huggingface.co/fishaudio/speech-lm-v1/blob/main/text2semantic-400m-v0.1-4k.pth -O checkpoints/text2semantic-400m-v0.1-4k.pth
```

### [可选] 从语音生成 prompt：
```bash
python tools/vqgan/inference.py -i paimon.wav --checkpoint-path checkpoints/vqgan-v1.pth
```

你应该能得到一个 `fake.npy` 文件。

### 从文本生成语义 token：
```bash
python tools/llama/generate.py \
    --text "要转换的文本" \
    --prompt-string "你的参考文本" \
    --prompt-tokens "fake.npy" \
    --checkpoint-path "checkpoints/text2semantic-400m-v0.1-4k.pth" \
    --num-samples 2 \
    --compile
```

该命令会在工作目录下创建 `codes_N` 文件，其中 N 是从 0 开始的整数。
您可能希望使用 `--compile` 来融合 cuda 内核以实现更快的推理（~30 个 token/秒 -> ~500 个 token/秒）。

### 从语义 token 生成人声：
```bash
python tools/vqgan/inference.py -i codes_0.npy --checkpoint-path checkpoints/vqgan-v1.pth
```

## Rust 数据服务器
由于加载和洗牌数据集非常缓慢且占用内存，因此我们使用 rust 服务器来加载和洗牌数据集。该服务器基于 GRPC，可以通过以下方式安装

```bash
cd data_server
cargo build --release
```

## 致谢
- [VITS2 (daniilrobnikov)](https://github.com/daniilrobnikov/vits2)
- [Bert-VITS2](https://github.com/fishaudio/Bert-VITS2)
- [GPT VITS](https://github.com/innnky/gpt-vits)
- [MQTTS](https://github.com/b04901014/MQTTS)
- [GPT Fast](https://github.com/pytorch-labs/gpt-fast)

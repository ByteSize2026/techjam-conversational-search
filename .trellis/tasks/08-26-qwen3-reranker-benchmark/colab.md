# Colab source ZIP

Colab 的 notebook 默认走本地 source ZIP 上传，因此第一阶段不依赖 GitHub 登录，也不需要 PAT/token。source ZIP 只应包含项目代码和公开的 `data/public_set.jsonl`；`catalog.jsonl` 在 notebook 的后续单独上传步骤提供。

在仓库根目录执行下面的命令生成 ZIP。`git archive HEAD` 只包含已经提交的 tracked files，不会把工作区里未提交的本地修改带进去：

```bash
git archive --format=zip \
  --output=/tmp/techjam-colab-code.zip \
  HEAD -- . \
  ':(exclude)catalog.jsonl' \
  ':(exclude)catalog.jsonl.gz' \
  ':(exclude)**/catalog.jsonl' \
  ':(exclude)**/catalog.jsonl.gz' \
  ':(exclude).claude' \
  ':(exclude).cursor' \
  ':(exclude)models' \
  ':(exclude)**/models/**' \
  ':(exclude)organizer' \
  ':(exclude)secure' \
  ':(exclude).env' \
  ':(exclude).env.*' \
  ':(exclude)**/.env' \
  ':(exclude)**/.env.*'
```

生成后检查 ZIP 内容，确认没有 catalog、模型权重、凭据或仓库元数据：

```bash
unzip -Z1 /tmp/techjam-colab-code.zip
unzip -Z1 /tmp/techjam-colab-code.zip | \
  rg '(^|/)(catalog\.jsonl(\.gz)?|\.env(\..*)?|\.git|\.claude|\.cursor)(/|$)|\.(bin|ckpt|gguf|onnx|pt|pth|safetensors|tflite|weights)$'
```

第二条命令没有输出才是预期结果。如果当前 CUDA benchmark 改动还未提交，先提交后再生成 ZIP；否则 notebook 会在第一阶段的 capability check 处明确拒绝旧代码。只把这个 source ZIP 上传到 notebook 的第一步，不要把 catalog 或模型文件混进 ZIP。随后按 notebook 提示单独上传唯一的 `catalog.jsonl`。

如果确认公开仓库可访问，也可以把 notebook 第一格的 `SOURCE_MODE` 改为 `"clone"`。clone 失败（尤其 HTTP 404）时 notebook 会立刻提示切回 ZIP 上传，不会要求登录凭据。

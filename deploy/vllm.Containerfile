# Optional external ASR server. Built separately from the application images.
# Add audio extras for exactly the vLLM version shipped in the latest image.
FROM docker.io/vllm/vllm-openai:latest
RUN uv pip install --system "vllm[audio]==$(python3 -c 'from importlib.metadata import version; print(version("vllm"))')"

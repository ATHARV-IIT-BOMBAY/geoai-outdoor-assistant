FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends build-essential cmake && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH PYTHONUNBUFFERED=1
WORKDIR /home/user/app
COPY --chown=user requirements.txt .
# portable CPU build: the build machine's CPU may differ from the one the Space runs on
RUN CMAKE_ARGS="-DGGML_NATIVE=OFF" pip install --no-cache-dir -r requirements.txt
# bake the weights into the image so a restart doesn't re-download them
RUN python -c "from huggingface_hub import hf_hub_download; hf_hub_download('AtharvIITB/geoai-outdoor-slm', 'Qwen3-1.7B-Q4_K_M.gguf', local_dir='models')"
COPY --chown=user app app
EXPOSE 7860
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]

FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y git python3 python3-pip python3-venv ffmpeg curl && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/moshi

# Clone the official Moshi repo (will be built inside the container)
ARG GIT_REPO="https://github.com/kyutai-labs/moshi.git"
RUN git clone $GIT_REPO .

# Install Python deps (may require adjustment depending on repo changes)
RUN pip3 install --upgrade pip
RUN if [ -f requirements.txt ]; then pip3 install -r requirements.txt || true; fi
RUN if [ -f requirements-dev.txt ]; then pip3 install -r requirements-dev.txt || true; fi
RUN pip3 install .

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8091
CMD ["/entrypoint.sh"]

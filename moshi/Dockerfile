FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y git python3 python3-pip python3-venv ffmpeg curl && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/moshi

# Installer Moshi depuis PyPI
RUN pip3 install --upgrade pip
RUN pip3 install -U moshi

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8091
CMD ["/entrypoint.sh"]

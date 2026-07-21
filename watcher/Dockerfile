FROM python:3.12-slim

RUN apt-get update && apt-get install -y curl ca-certificates && \
    curl -sL https://aka.ms/InstallAzureCLIDeb | bash && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

RUN curl -fSL --retry 5 --retry-delay 5 --retry-all-errors \
         --connect-timeout 15 --max-time 60 \
         "https://dl.k8s.io/release/$(curl -fsSL --retry 5 https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
         -o /usr/local/bin/kubectl && \
    chmod +x /usr/local/bin/kubectl && \
    kubectl version --client

WORKDIR /app

RUN pip install --no-cache-dir requests slack_sdk

COPY watcher.py .

CMD ["python", "-u", "watcher.py"]

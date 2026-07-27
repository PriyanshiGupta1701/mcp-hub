FROM python:3.12-slim

RUN apt-get update && apt-get install -y curl ca-certificates git && \
    curl -sL https://aka.ms/InstallAzureCLIDeb | bash && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y \
    git \
    openjdk-21-jre-headless \
    unzip \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fSL --retry 5 --retry-delay 5 --retry-all-errors \
         --connect-timeout 15 --max-time 120 \
         https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/sonar-scanner-cli-5.0.1.3006-linux.zip \
         -o /tmp/sonar-scanner.zip && \
    unzip -q /tmp/sonar-scanner.zip -d /opt && \
    rm /tmp/sonar-scanner.zip && \
    mv /opt/sonar-scanner-* /opt/sonar-scanner && \
    ln -s /opt/sonar-scanner/bin/sonar-scanner /usr/local/bin/sonar-scanner

RUN curl -fSL --retry 5 --retry-delay 5 --retry-all-errors \
         --connect-timeout 15 --max-time 60 \
         "https://dl.k8s.io/release/$(curl -fsSL --retry 5 https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
         -o /usr/local/bin/kubectl && \
    chmod +x /usr/local/bin/kubectl && \
    kubectl version --client

WORKDIR /app

RUN pip install --no-cache-dir requests slack_sdk

COPY main.py .
COPY watcher/ ./watcher/

CMD ["python", "-u", "main.py"]

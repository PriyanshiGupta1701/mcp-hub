# FROM us-central1-docker.pkg.dev/genuine-flight-317411/devel/holmes

# USER root

# # Install Azure CLI
# # RUN apt-get update && apt-get install -y curl ca-certificates lsb-release && \
# #     curl -fSL https://aka.ms/InstallAzureCLIDeb -o /tmp/install-az.sh && \
# #     bash /tmp/install-az.sh && \
# #     rm /tmp/install-az.sh && \
# #     which az && \
# #     apt-get clean && rm -rf /var/lib/apt/lists/*

# RUN apt-get update && apt-get install -y curl ca-certificates && \
#     curl -sL https://aka.ms/InstallAzureCLIDeb | bash && \
#     apt-get clean && rm -rf /var/lib/apt/lists/


# # Wrap the real az binary so no call (e.g. an unbounded 'log tail') can
# # hang the Holmes agent loop indefinitely — see az-wrapper.sh for details.
# RUN mv /usr/bin/az /usr/bin/az.real
# COPY az-wrapper.sh /usr/bin/az
# RUN chmod +x /usr/bin/az

# # Install azure-api-mcp binary
# RUN curl -Lo /usr/local/bin/azure-api-mcp \
#     https://github.com/Azure/azure-api-mcp/releases/latest/download/azure-api-mcp-linux-amd64 \
#     && chmod +x /usr/local/bin/azure-api-mcp

# # Add startup script
# COPY start.sh /start.sh
# RUN chmod +x /start.sh

FROM us-central1-docker.pkg.dev/genuine-flight-317411/devel/holmes

USER root

# Disable unreachable Kubernetes repo from base image
RUN mv /etc/apt/sources.list.d/kubernetes.list \
       /etc/apt/sources.list.d/kubernetes.list.disabled

RUN apt-get update && \
    apt-get install -y curl ca-certificates && \
    curl -sL https://aka.ms/InstallAzureCLIDeb | bash && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN mv /usr/bin/az /usr/bin/az.real

COPY az-wrapper.sh /usr/bin/az
RUN chmod +x /usr/bin/az

RUN curl -Lo /usr/local/bin/azure-api-mcp \
    https://github.com/Azure/azure-api-mcp/releases/latest/download/azure-api-mcp-linux-amd64 \
    && chmod +x /usr/local/bin/azure-api-mcp

COPY start.sh /start.sh
RUN chmod +x /start.sh
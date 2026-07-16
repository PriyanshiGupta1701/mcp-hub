#!/bin/sh
set -e

# Log into Azure using service principal if credentials are available
if [ -n "$AZURE_TENANT_ID" ] && [ -n "$AZURE_CLIENT_ID" ] && [ -n "$AZURE_CLIENT_SECRET" ]; then
    echo "Logging into Azure..."
    az login --service-principal \
        --tenant "$AZURE_TENANT_ID" \
        --username "$AZURE_CLIENT_ID" \
        --password "$AZURE_CLIENT_SECRET" \
        --output none
    
    if [ -n "$AZURE_SUBSCRIPTION_ID" ]; then
        az account set --subscription "$AZURE_SUBSCRIPTION_ID"
    fi
    echo "Azure login successful"
else
    echo "Azure credentials not set, skipping az login"
fi

# Run the original Holmes entrypoint
mkdir -p /root/.kube
if [ -f /tmp/.kube/config ] && [ -s /tmp/.kube/config ]; then
    cp /tmp/.kube/config /root/.kube/config
    sed -i 's|server: https://127\.0\.0\.1|server: https://host.docker.internal|g; s|server: https://localhost|server: https://host.docker.internal|g' /root/.kube/config
else
    echo "No kubeconfig found, skipping"
fi

exec python -u server.py
FROM docker.io/library/python:3.13.2-slim

RUN export DEBIAN_FRONTEND=noninteractive && \
    apt-get update && \
    apt-get --no-install-suggests --no-install-recommends -o Dpkg::Options::="--force-confnew" --allow-downgrades --allow-remove-essential --allow-change-held-packages -fuy \
        install git gpg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /glci

COPY requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt
COPY glci/ glci/
COPY *.py ./
RUN ln -s .cicd-cli.py ls-manifests && \
    ln -s .cicd-cli.py publish-release-set && \
    ln -s .cicd-cli.py cleanup-release-set
COPY publishing-cfg.yaml package_aliases.yaml flavours.yaml ./
ENV SECRET_CIPHER_ALGORITHM=AES.ECB SECRETS_SERVER_ENDPOINT=TRUE

ENV CREDENTIALS_JSON_PATH=/gardenlinux-credentials.json CREDENTIALS_JSON_GPG_PATH=/gardenlinux-credentials.json.gpg CREDENTIALS_KEY=''

COPY entrypoint.sh /
ENTRYPOINT ["/entrypoint.sh"]

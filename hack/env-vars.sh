# !!!! Make sure you have the required repositories in correct Paths !!!! 

# github.com/gardenlinux/builder 
export GARDENLINUX_BUILDER_PATH=$PWD/../builder

# github.com/gardenlinux/gardenlinux
export GARDENLINUX_PATH=$PWD/../gardenlinux

# For access to this repo, please reach out to your glci Maintainer colleagues
# This repo guides you to through steps to setup gardenlinux-crendentials.json file
export CC_CONFIG_DIR=$PWD/../cfg-gardenlinux

# For generating credentials file, please reach out to glci Maintainers colleagues
export SECRETS_SERVER_CACHE=$PWD/../gardenlinux-credentials.json
export SECRET_CIPHER_ALGORITHM=AES.ECB
export SECRETS_SERVER_ENDPOINT=TRUE

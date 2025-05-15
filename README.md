# Gardenlinux image publishing gear

This repository contains tooling and configuration for publishing gardenlinux images as machine
images to different hyperscalers.

The images to be published are built in a separate pipeline from sources hosted in
[gardenlinux repository](https://github.com/gardenlinux/gardenlinux), and consumed from a
S3 bucket.

# Container

```shell
podman run --rm -it -v /path/to/gardenlinux-credentials.json:/gardenlinux-credentials.json:ro ghcr.io/gardenlinux/glci /glci/publish-release-set --cfg-name gardener-integration-test --version "$version" --commit "$commitish"
```

# (Local) Setup

- install python-package `gardener-cicd-libs` (requires "build-essentials" (gcc, ..))
- alternative: manually install from https://github.com/gardener/cc-utils
- install additional python-packages from Dockerfile
- clone https://github.com/gardenlinux/gardenlinux in a sibling directory `gardenlinux` to this repo
- install SAP root certificates (see instructions [below](#install-sap-certs)


See `Credential Handling` below for details of how to pass secrets to publishing-script.

# CLI Reference / Guide

**find available release-manifests**

Use `ls-manifests` to list existing manifests.

Consider using `--versions` or `--versions-and-commits` flags for conveniently printing required
selectors (gardenlinux-version and build-commit) for image-publishing.

Use `--version-prefix` to narrow down search.

**publish images for consumption through gardener**

Use `publish-release-set` to start image-publishing. Note that a full release will require some few
GiBs of data download and upload, and will take several tens of minutes.

Use aforementioned `ls-manifests` command to determine valid combinations of `--version` and
`--commit`. Optionally use `--flavourset-name` and `--flavours-file` to specify different
platforms and build flavours (defaults to preset for "Gardener").

Any additional parameters are intended for debugging / testing purposes.

## Credential Handling

The build pipeline can be used with a central server managing configuration and
secrets. As an alternative all credentials can be read from a Kubernetes secret
named "secrets" in the corresponding namespace. This secret will be
automatically generated from configuration files. The switch between central
server and a Kubernetes secret is done by an environment variable named
`SECRET_SERVER_ENDPOINT`. If it is not set the secret will be generated and
applied. At minimum there need to be two secrets: One for uploading the
artifacts to an S3-like Object store and one to upload container images to an
OCI registry. Example files are provided in the folder `ci/cfg`.

Edit the files cfg/cfg_types.yaml. Each top-level entry refers to another file
containing the credentials. Examples with templates are provided. A second
entry is for uploading the base-image and to an OCI registry. Additional
configuration information is found in [publishing-cfg.yaml](publishing-cfg.yaml)

For sending notifications by default recipients are read from the CODEOWNERS
files. Resolving this to email requires access to the Github API which is not
possible for external users. The behavior can be overriden by setting the
variable `only_recipients` in the pipelineRun file. If this variable contains a
semicolon separated list of email addresses emails are sent only to these
recipients. CODEWONWERS access is not needed then. For configuring an SMTP
server a sample file is provided.

## Install SAP certs
Since glci depends on the gardener cicd libs, which in turn requires SAP's root
CA's locally trusted, we also need to add those when running glci. 
In case of pipeline runs, we have this already installed in the job image 
(e.g. [here](https://github.com/gardener/cc-utils/blob/7ed9d6575cbe83ef1e04110b0e743ffc21a8ced7/Dockerfile.job-image-base#L51)). 
In case you want to run glci locally, you need to install those 
manually on your system, as described below.

The global root CA can be downloaded from [here](https://sapcerts.wdf.global.corp.sap/CandP.aspx)
You will need the follwing two CRT files:
- [SapNetCA_G2_2](https://aia.pki.co.sap.com/aia/SAPNetCA_G2_2.crt)
- [Sap Global Root](https://aia.pki.co.sap.com/aia/SAP%20Global%20Root%20CA.crt)

Once downloaded, you need to add the certificates to your local trusted certificates file.
To find the file location of the trusted certificates, you can use the following command:
```
CA_CERT_PEM_FILE=$(python -c 'import certifi; print(certifi.where()) 
``` 

Append the downloaded certificates to the trusted certificates file:
```
cat "SAPNetCA_G2_2.crl" >> $CA_CERT_PEM_FILE
cat "SAP Global Root CA.crt" >> $CA_CERT_PEM_FILE
```

## Integration Tests (under construction)

The integration test are implemented as their own tekton task which can be
found [here](./integrationtest-task.yaml).  The test automatically clones the
github repo specified in the tekton resource and executes the integration test
with the specified version (branch or commit).

The task assumes that there is a secret in the cluster with the following
structure:

```yaml
---
apiVersion: v1
kind: Secret
metadata:
  name: github-com-user
  annotations:
    tekton.dev/git-0: https://github.com
type: kubernetes.io/basic-auth
stringData:
  username: <github username>
  password: <github password>
```

The test can be executed within a cluster that has tekton installed by running:

```
# create test defintions and resources
kubectl apply -f ./ci/integrationtest-task.yaml

# run the actual test as taskrun
kubectl create -f ./ci/it-run.yaml
```
Running the integration tests is work-in-progress.

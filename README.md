# Gardenlinux image publishing gear

This repository contains tooling and configuration for publishing gardenlinux images as machine
images to different hyperscalers.

The images to be published are built in a separate pipeline from sources hosted in
[gardenlinux repository](https://github.com/gardenlinux/gardenlinux), and consumed from a
S3 bucket.

# Container

```shell
podman run --rm -it -v /path/to/gardenlinux-credentials.json:/gardenlinux-credentials.json:ro glci /glci/publish-release-set --cfg-name gardener-integration-test --version "$version" --commit "$commitish"
```

# (Local) Setup

- install python-package `gardener-cicd-libs` (requires "build-essentials" (gcc, ..))
- alternative: manually install from https://github.com/gardener/cc-utils
- install additional python-packages from Dockerfile
- clone https://github.com/gardenlinux/gardenlinux in a sibling directory `gardenlinux` to this repo

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

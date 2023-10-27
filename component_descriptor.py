import logging
import typing

import glci
import glci.model
import glci.s3
import glci.util

import ccc.aws
import gci.componentmodel as cm

import cnudie.retrieve

logger = logging.getLogger(__name__)


def _iter_debian_packages(
    release_manifest,
    s3_client,
) -> typing.Generator[str, None, None]:
    manifest_file_path = release_manifest.path_by_suffix('.manifest')
    resp = s3_client.get_object(
        Bucket=manifest_file_path.s3_bucket_name,
        Key=manifest_file_path.s3_key,
    )

    for line in resp['Body'].iter_lines():
        yield line.decode('utf-8')


def iter_resources(
    release_manifests: list[glci.model.OnlineReleaseManifest],
    version: str,
    s3_client,
):
    for release_manifest in release_manifests:
        yield virtual_machine_image_resource(
            release_manifest=release_manifest,
            version=version,
            s3_client=s3_client,
        )
        yield _image_rootfs_resource(
            release_manifest=release_manifest,
            s3_client=s3_client,
            version=version,
        )


def component_descriptor(
    version: str,
    commit: str,
    publishing_cfg: glci.model.PublishingCfg,
    release_manifests: list[glci.model.OnlineReleaseManifest],
    cfg_factory,
) -> cm.ComponentDescriptor:
    ocm_cfg = cfg_factory.ctx_repository(publishing_cfg.ocm.component_repository_cfg_name)
    ocm_repo_base_url = ocm_cfg.base_url()

    s3_session = ccc.aws.session(publishing_cfg.origin_buildresult_bucket.aws_cfg_name)
    s3_client = s3_session.client('s3')

    component_descriptor = cm.ComponentDescriptor(
        meta=cm.Metadata(schemaVersion=cm.SchemaVersion.V2),
        component=cm.Component(
            name='github.com/gardenlinux/gardenlinux',
            version=version,
            repositoryContexts=[
                cm.OciRepositoryContext(
                    baseUrl=ocm_repo_base_url,
                    type=cm.AccessType.OCI_REGISTRY,
                )
            ],
            provider='sap-se',
            sources=[
                cm.ComponentSource(
                    name='gardenlinux',
                    type=cm.SourceType.GIT,
                    access=cm.GithubAccess(
                        type=cm.AccessType.GITHUB,
                        repoUrl='https://github.com/gardenlinux/gardenlinux',
                        ref='refs/heads/main', # TODO: determine release-tag
                        commit=commit,
                    ),
                    version=version,
                    labels=[
                        cm.Label(
                            name='cloud.gardener.cnudie/dso/scanning-hints/source_analysis/v1',
                            value={
                                'policy': 'skip',
                                'comment': 'repo only contains build instructions, source in this repo will not get incorporated into the final artefact'
                            }
                        )
                    ],
                )
            ],
            componentReferences=[],
            resources=[
                r for r
                in iter_resources(
                    release_manifests=release_manifests,
                    version=version,
                    s3_client=s3_client,
                )
            ],
        ),
    )

    return component_descriptor


def retrieve_component(
        ctx_repository: str,
        component_name: str,
        component_version: str,
):
    lookup = cnudie.retrieve.create_default_component_descriptor_lookup(
        default_ctx_repo=cm.OciRepositoryContext(baseUrl=ctx_repository),
    )

    gardenlinux_component_descriptor = lookup(
        cm.ComponentIdentity(
            name=component_name,
            version=component_version,
        )
    )
    return gardenlinux_component_descriptor.component


def virtual_machine_image_resource(
    release_manifest: glci.model.OnlineReleaseManifest,
    version: str,
    s3_client,
):
    labels = [
        cm.Label(
            name='gardener.cloud/gardenlinux/ci/build-metadata',
            value={
                'modifiers': release_manifest.modifiers,
                'buildTimestamp': release_manifest.build_timestamp,
            }
        ),
    ]

    s3_client = s3_client
    packages = _iter_debian_packages(
        release_manifest,
        s3_client=s3_client,
    )
    package_aliases = glci.util.package_aliases()
    package_versions = []

    for package in packages:
        match package.split(' '):
            case [name, package_version]:
                package_versions.append({
                    'name': name,
                    'aliases': package_aliases.get(name) or [],
                    'version': package_version,
                })
            case _:
                logger.warning(
                    f'Unable to parse package-string {package}. No version-information will be '
                    'added to the component-descriptor for this package.'
                )

    if package_versions:
        labels.append(
            cm.Label(
                name='cloud.cnudie/dso/scanning-hints/package-versions',
                value=package_versions,
            )
        )

    if (published_image_metadata := release_manifest.published_image_metadata):
        labels.append(
            cm.Label(
                name='gardener.cloud/gardenlinux/ci/published-image-metadata',
                value=published_image_metadata,
            ),
        )

    image_file_suffix = glci.util.vm_image_artefact_for_platform(release_manifest.platform)
    image_file_path = release_manifest.path_by_suffix(image_file_suffix)
    resource_access = cm.S3Access(
        type=cm.AccessType.S3,
        bucketName=release_manifest.s3_bucket,
        objectKey=image_file_path.s3_key,
    )

    return cm.Resource(
        name='gardenlinux',
        version=version,
        extraIdentity={
            'feature-flags': ','.join(release_manifest.modifiers),
            'architecture': release_manifest.architecture,
            'platform': release_manifest.platform,
        },
        type='virtual_machine_image',
        labels=labels,
        access=resource_access,
        digest=cm.DigestSpec(
            hashAlgorithm='NO-DIGEST',
            normalisationAlgorithm='EXCLUDE-FROM-SIGNATURE',
            value='NO-DIGEST',
        ),
    )


def _image_rootfs_resource(
    release_manifest: glci.model.OnlineReleaseManifest,
    s3_client,
    version: str,
):
    labels = [
        cm.Label(
          name='gardener.cloud/gardenlinux/ci/build-metadata',
          value={
              'modifiers': release_manifest.modifiers,
              'buildTimestamp': release_manifest.build_timestamp,
              'debianPackages': [
                  p for p
                  in _iter_debian_packages(
                      release_manifest,
                      s3_client=s3_client,
                  )
              ],
          }
        ),
        cm.Label(
            name='cloud.gardener.cnudie/responsibles',
            value=[
                {
                    'type': 'emailAddress',
                    'email': 'thomas.buchner@sap.com',
                },
            ],
        ),
    ]

    rootfs_file_path = release_manifest.path_by_suffix('.tar')

    return cm.Resource(
        name='rootfs',
        version=version,
        extraIdentity={
            'feature-flags': ','.join(release_manifest.modifiers),
            'architecture': release_manifest.architecture,
            'platform': release_manifest.platform,
        },
        type='application/tar+vm-image-rootfs',
        labels=labels,
        access=cm.S3Access(
            type=cm.AccessType.S3,
            bucketName=release_manifest.s3_bucket,
            objectKey=rootfs_file_path.s3_key,
        ),
        digest=cm.DigestSpec(
            hashAlgorithm='NO-DIGEST',
            normalisationAlgorithm='EXCLUDE-FROM-SIGNATURE',
            value='NO-DIGEST',
        ),
    )


def release_manifest_set_resource(
    cicd_cfg,
    effective_version: str,
    manifest_set_s3_key: str,
):
    bucket_name = cicd_cfg.build.s3_bucket_name

    resource_access = cm.S3Access(
        type=cm.AccessType.S3,
        bucketName=bucket_name,
        objectKey=manifest_set_s3_key,
    )

    return cm.Resource(
        name='release_manifest_set',
        version=effective_version,
        type='release_manifest_set',
        access=resource_access,
        digest=cm.DigestSpec(
            hashAlgorithm='NO-DIGEST',
            normalisationAlgorithm='EXCLUDE-FROM-SIGNATURE',
            value='NO-DIGEST',
        ),
    )

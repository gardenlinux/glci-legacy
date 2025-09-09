import concurrent.futures
import dataclasses
import datetime
import enum
import functools
import io
import logging
import os
import pprint
import typing

import botocore.client
import botocore.exceptions
import dacite
import yaml

import glci.aws
import glci.model
import paths

import dacite.exceptions

GardenlinuxFlavourSet = glci.model.GardenlinuxFlavourSet
GardenlinuxFlavour = glci.model.GardenlinuxFlavour
GardenlinuxFlavourCombination = glci.model.GardenlinuxFlavourCombination
Architecture = glci.model.Architecture

PublishingCfg = glci.model.PublishingCfg

logger = logging.getLogger(__name__)


def publishing_cfg(
    cfg_name: str='default',
    cfg_file=paths.publishing_cfg_path,
) -> PublishingCfg:
    with open(cfg_file) as f:
        parsed = yaml.safe_load(f)

    for cfg in parsed:
        cfg = dacite.from_dict(
            data_class=PublishingCfg,
            data=cfg,
            config=dacite.Config(cast=[enum.Enum]),
        )
        if cfg.name == cfg_name:
            return cfg
    else:
        raise ValueError(f'not found: {cfg_name=}')


def flavour_sets(
    build_yaml: str=paths.flavour_cfg_path,
) -> typing.List[GardenlinuxFlavourSet]:
    with open(build_yaml) as f:
        parsed = yaml.safe_load(f)

    sets = [
        dacite.from_dict(
            data_class=GardenlinuxFlavourSet,
            data=fset,
            config=dacite.Config(
                cast=[Architecture, typing.Tuple]
            )
        ) for fset in parsed['flavour_sets']
    ]

    return sets


def flavour_set(
    flavour_set_name: str,
    build_yaml: str=paths.flavour_cfg_path,
) -> GardenlinuxFlavourSet:
    for fs in flavour_sets(build_yaml=build_yaml):
        if fs.name == flavour_set_name:
            return fs
    else:
        raise RuntimeError(f'not found: {flavour_set_name=}')


def release_manifest(
    s3_client: botocore.client.BaseClient,
    bucket_name: str,
    key: str,
    absent_ok: bool=False,
) -> glci.model.OnlineReleaseManifest | None:
    """
    retrieves and deserialises a gardenlinux release manifest from the specified s3 object
    (expects a YAML or JSON document)
    """
    buf = io.BytesIO()
    try:
        s3_client.download_fileobj(
            Bucket=bucket_name,
            Key=key,
            Fileobj=buf,
        )
    except botocore.exceptions.ClientError as e:
        if absent_ok and str(e.response['Error']['Code']) == '404':
            return None
        raise e

    buf.seek(0)
    parsed = yaml.safe_load(buf)

    # patch-in transient attrs
    parsed['s3_key'] = key
    parsed['s3_bucket'] = bucket_name
    if not 'base_image' in parsed:
        parsed['base_image'] = None

    try:
        manifest = dacite.from_dict(
            data_class=glci.model.OnlineReleaseManifest,
            data=parsed,
            config=dacite.Config(
                cast=[
                    glci.model.Architecture,
                    typing.Tuple,
                    glci.model.TestResultCode,
                    glci.model.AzureTransportState,
                    glci.model.AzureHyperVGeneration,
                ],
            ),
        )
    except dacite.exceptions.UnionMatchError as e:
        raise e

    return manifest


def release_manifest_set(
    s3_client: botocore.client.BaseClient,
    bucket_name: str,
    manifest_key: str,
    absent_ok: bool=False,
) -> glci.model.ReleaseManifestSet | None:
    buf = io.BytesIO()
    try:
        s3_client.download_fileobj(
            Bucket=bucket_name,
            Key=manifest_key,
            Fileobj=buf,
        )
    except botocore.exceptions.ClientError as e:
        if absent_ok and str(e.response['Error']['Code']) == '404':
            return None
        raise e

    buf.seek(0)
    parsed = yaml.safe_load(buf)

    parsed['s3_bucket'] = bucket_name
    parsed['s3_key'] = manifest_key

    logger.debug(manifest_key)
    manifest_set = dacite.from_dict(
        data_class=glci.model.OnlineReleaseManifestSet,
        data=parsed,
        config=dacite.Config(
            cast=[
                glci.model.Architecture,
                typing.Tuple,
                glci.model.TestResultCode,
                glci.model.AzureTransportState,
                glci.model.AzureHyperVGeneration,
            ],
        ),
    )
    return manifest_set


def _json_serialisable_manifest(obj: typing.Any):
    # workaround: need to convert enums to str recursively
    # Note this is not a generic implementation, sequences etc. are not converted
    if hasattr(obj, '__dict__'):
        if not dataclasses.is_dataclass(obj):
            raise TypeError(f'cannot json-serialize non dataclass object: {obj}')
        patch_args = {}
        for attr, val in obj.__dict__.items():
            if isinstance(val, enum.Enum):
                patch_args[attr] = val.value
            elif dataclasses.is_dataclass(val):
                patch_args[attr] = _json_serialisable_manifest(val)
        if patch_args:
            obj = dataclasses.replace(obj, **patch_args)
    return obj


def upload_release_manifest(
    s3_client: botocore.client.BaseClient,
    bucket_name: str,
    key: str,
    manifest: glci.model.ReleaseManifest,
):
    manifest = _json_serialisable_manifest(obj=manifest)
    manifest_bytes = yaml.safe_dump(dataclasses.asdict(manifest)).encode('utf-8')
    manifest_fobj = io.BytesIO(initial_bytes=manifest_bytes)
    return s3_client.upload_fileobj(
        Fileobj=manifest_fobj,
        Bucket=bucket_name,
        Key=key,
        ExtraArgs={
            'ContentType': 'text/yaml',
            'ContentEncoding': 'utf-8',
        },
    )


def upload_release_manifest_set(
    s3_client: botocore.client.BaseClient,
    bucket_name: str,
    key: str,
    manifest_set: glci.model.ReleaseManifestSet,
):
    manifests = (_json_serialisable_manifest(m) for m in manifest_set.manifests)
    manifest_set = dataclasses.replace(manifest_set, manifests=tuple(manifests))

    manifest_set_bytes = yaml.safe_dump(dataclasses.asdict(manifest_set)).encode('utf-8')
    manifest_set_fobj = io.BytesIO(initial_bytes=manifest_set_bytes)

    return s3_client.upload_fileobj(
        Fileobj=manifest_set_fobj,
        Bucket=bucket_name,
        Key=key,
        ExtraArgs={
            'ContentType': 'text/yaml',
            'ContentEncoding': 'utf-8',
        },
    )


def enumerate_releases(
    s3_client: botocore.client.BaseClient,
    bucket_name: str,
    prefix: str=glci.model.ReleaseManifest.manifest_key_prefix,
) -> typing.Generator[glci.model.ReleaseManifest, None, None]:
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=64)
    _release_manifest = functools.partial(
        release_manifest,
        s3_client=s3_client,
        bucket_name=bucket_name,
    )

    continuation_token = None
    while True:
        ctoken_args = {'ContinuationToken': continuation_token} \
                if continuation_token \
                else {}

        res = s3_client.list_objects_v2(
            Bucket=bucket_name,
            Prefix=prefix,
            **ctoken_args,
        )
        if (key_count := res['KeyCount']) == 0:
            return
        is_truncated = bool(res['IsTruncated'])
        continuation_token = res.get('NextContinuationToken')

        logger.info(f'found {key_count} release manifests')

        def wrap_release_manifest(key):
            return _release_manifest(key=key)

        keys = [obj_dict['Key'] for obj_dict in res['Contents']]

        yield from executor.map(wrap_release_manifest, keys)

        if not is_truncated:
            return


def find_release(
    s3_client: botocore.client.BaseClient,
    bucket_name: str,
    release_identifier: glci.model.ReleaseIdentifier,
) -> typing.Optional[glci.model.OnlineReleaseManifest]:
    normalised = glci.model.normalised_release_identifier
    release_manifest_key = release_identifier.canonical_release_manifest_key()

    manifest = release_manifest(
        s3_client=s3_client,
        bucket_name=bucket_name,
        key=release_manifest_key,
        absent_ok=True,
    )

    if not manifest:
        return None

    if (found_ri := normalised(manifest.release_identifier())) \
        == (searched_ri := normalised(release_identifier)):
        return manifest
    else:
        # warn about not matching expected contents from canonical name
        logger.warning(f'{release_manifest_key=} contained unexpected contents:')
        logger.warning('this is the release-identifier we searched for:')
        logger.warning(pprint.pformat(dataclasses.asdict(searched_ri)))
        logger.warning('this is the release-identifier we found:')
        logger.warning(pprint.pformat(dataclasses.asdict(found_ri)))

        return None


def find_releases(
    s3_client: botocore.client.BaseClient,
    bucket_name: str,
    fset: glci.model.GardenlinuxFlavourSet,
    build_committish: str,
    version: str,
    gardenlinux_epoch: int,
) -> typing.Generator[glci.model.OnlineReleaseManifest, None, None]:
    flavours = set(fset.flavours())

    for flavour in flavours:
        release_identifier = glci.model.ReleaseIdentifier(
            build_committish=build_committish,
            version=version,
            gardenlinux_epoch=gardenlinux_epoch,
            architecture=flavour.architecture,
            platform=flavour.platform,
            modifiers=flavour.modifiers,
        )

        existing_release = find_release(
            s3_client=s3_client,
            bucket_name=bucket_name,
            release_identifier=release_identifier,
        )

        if existing_release:
            yield existing_release


def release_set_manifest_name(
    build_committish: str,
    gardenlinux_epoch: int,
    version: str,
    flavour_set_name: str,
    build_type: glci.model.BuildType,
    with_timestamp: bool = False,
):
    bt = glci.model.BuildType

    if build_type in (bt.SNAPSHOT, bt.DAILY):
        name = f'{gardenlinux_epoch}-{build_committish[:6]}-{flavour_set_name}'
        if with_timestamp:
            name += '-' + datetime.datetime.now(datetime.UTC).strftime('%Y%m%d-%H%M%S')
    elif build_type is bt.RELEASE:
        name = f'{version}-{flavour_set_name}'
    else:
        raise ValueError(build_type)

    return name


def enumerate_release_sets(
    s3_client: botocore.client.BaseClient,
    bucket_name: str,
    prefix: str=glci.model.ReleaseManifestSet.release_manifest_set_prefix,
) -> typing.Generator[glci.model.ReleaseManifestSet, None, None]:
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=16)
    _release_manifest_set = functools.partial(
        release_manifest_set,
        s3_client=s3_client,
        bucket_name=bucket_name,
    )

    continuation_token = None
    while True:
        ctoken_args = {'ContinuationToken': continuation_token} \
                if continuation_token \
                else {}

        res = s3_client.list_objects_v2(
            Bucket=bucket_name,
            Prefix=prefix,
            **ctoken_args,
        )
        if (key_count := res['KeyCount']) == 0:
            return
        is_truncated = bool(res['IsTruncated'])
        continuation_token = res.get('NextContinuationToken')

        logger.info(f'found {key_count} release manifests')

        keys = [
            key for obj_dict in res['Contents']
            # filter out directories
            if s3_client.head_object(
              Bucket=bucket_name,
              Key=(key := obj_dict['Key']),
            )['ContentType'] != 'application/x-directory'
        ]

        def wrap_release_manifest_set(key):
          return _release_manifest_set(manifest_key=key)

        yield from executor.map(wrap_release_manifest_set, keys)

        if not is_truncated:
            return


def find_release_set(
    s3_client: botocore.client.BaseClient,
    bucket_name: str,
    flavour_set_name: str,
    build_committish: str,
    gardenlinux_epoch: int,
    version: str,
    build_type: glci.model.BuildType,
    prefix: str=glci.model.ReleaseManifestSet.release_manifest_set_prefix,
    absent_ok=False,
) -> glci.model.ReleaseManifestSet:
    build_type = glci.model.BuildType(build_type)

    manifest_key = os.path.join(
        prefix,
        build_type.value,
        release_set_manifest_name(
            build_committish=build_committish,
            gardenlinux_epoch=gardenlinux_epoch,
            version=version,
            flavour_set_name=flavour_set_name,
            build_type=build_type,
        ),
    )

    logger.debug(manifest_key)

    manifest = release_manifest_set(
        s3_client=s3_client,
        bucket_name=bucket_name,
        manifest_key=manifest_key,
        absent_ok=absent_ok,
    )

    return manifest


class EnumValueYamlDumper(yaml.SafeDumper):
    """
    a yaml.SafeDumper that will dump enum objects using their values
    """
    def represent_data(self, data):
        if isinstance(data, enum.Enum):
            return self.represent_data(data.value)
        return super().represent_data(data)


def vm_image_artefact_for_platform(platform: glci.model.Platform) -> str:
    # map each platform to the suffix/object that is of interest.

    platform_to_artifact_mapping = {
        'ali': '.qcow2',
        'aws': '.raw',
        'azure': '.vhd',
        'gcp': '.gcpimage.tar.gz',
        'kvm': '.raw',
        'metal': '.tar.xz',
        'oci': '.tar.xz',
        'openstack': '.vmdk',
        'openstackbaremetal': '.vmdk',
        'vmware': '.ova',
    }

    if not platform in platform_to_artifact_mapping:
        raise NotImplementedError(
            f"No information about release artifacts available for platform '{platform}'"
        )

    return platform_to_artifact_mapping[platform]


def package_aliases(package_alias_file: str = paths.package_alias_path) -> dict:
    with open(package_alias_file) as f:
        parsed = yaml.safe_load(f)
    return parsed.get('aliases', {})

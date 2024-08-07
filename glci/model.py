from __future__ import annotations
import dataclasses
import datetime
import dateutil.parser
import enum
import functools
import itertools
import os
import subprocess
import typing

import dacite
import yaml

import paths

own_dir = os.path.abspath(os.path.dirname(__file__))
repo_root = os.path.abspath(os.path.join(
    own_dir, os.path.pardir, os.path.pardir))


class BuildTarget(enum.Enum):
    # compile, link, create arifacts local
    BUILD = ('build',
        {}
    )
    # upload artifacts to S3, create manifest
    MANIFEST = ('manifest',
        {'build', }
    )
    # create and upload component descr
    COMPONENT_DESCRIPTOR = ('component-descriptor',
        {'build', }
    )
    # run gardenlinux integration tests
    TESTS = ('tests',
        {'build', }
    )
    # upload images to cloud providers
    PUBLISH = ('publish',
        {'build', 'manifest', 'component-descriptor'}
    )
    # use version epoch.y.z instead of epoch-commit
    FREEZE_VERSION = ('freeze-version',
        {'build-baseimage', 'build', 'manifest', 'component-descriptor'}
    )
    # create a github release (branch, tag, release)
    GITHUB_RELEASE = ('github-release',
        {'build-baseimage', 'build', 'manifest', 'component-descriptor', 'freeze-version'}
    )

    def __new__(cls, value, requires=None):
        obj = object.__new__(cls)
        obj._value_ = value
        obj._requires_ = requires
        return obj

    @staticmethod
    def set_from_str(comma_separated: str) -> typing.Set[BuildTarget]:
        targets = {BuildTarget(action.strip()) for action in comma_separated.split(',')}
        BuildTarget.check_requirements(targets)
        return targets

    @staticmethod
    def check_requirements(bt_set: typing.Set[BuildTarget]):
        for e in bt_set:
            missing = set()
            for r in e._requires_:
                target = BuildTarget(r)
                if not target in bt_set:
                    missing.add(r)
            if missing:
                raise ValueError(f' {e.value}: missing required build target(s): {missing}')


class FeatureType(enum.Enum):
    '''
    gardenlinux feature types as used in `features/*/info.yaml`

    Each gardenlinux flavour MUST specify exactly one platform and MAY
    specify an arbitrary amount of modifiers.
    '''
    PLATFORM = 'platform'
    MODIFIER = 'modifier'


# TODO: Check feasibility of using proper enum(s)
Platform = str  # see `features/*/info.yaml` / platforms() for allowed values
Modifier = str  # see `features/*/info.yaml` / modifiers() for allowed values


@dataclasses.dataclass(frozen=True)
class Features:
    '''
    a FeatureDescriptor's feature cfg (currently, references to other features, only)
    '''
    include: typing.Tuple[Modifier, ...] = tuple()


@dataclasses.dataclass(frozen=True)
class FeatureDescriptor:
    '''
    A gardenlinux feature descriptor (parsed from $repo_root/features/*/info.yaml)
    '''
    type: FeatureType
    name: str
    description: str = 'no description available'
    features: Features = None

    def included_feature_names(self) -> typing.Tuple[Modifier, ...]:
        '''
        returns the tuple of feature names immediately depended-on by this feature
        '''
        if not self.features:
            return ()
        return self.features.include

    def included_features(self,
                          transitive=True
                          ) -> typing.Generator['FeatureDescriptor', None, None]:
        '''
        returns the tuple of features (transtively) included by this feature
        '''
        included_features = (feature_by_name(name)
                             for name in self.included_feature_names())

        for included_feature in included_features:
            if transitive:
                yield from included_feature.included_features()
            yield included_feature


class Architecture(enum.Enum):
    '''
    gardenlinux' target architectures, following Debian's naming
    '''
    AMD64 = 'amd64'
    ARM64 = 'arm64'


@dataclasses.dataclass(frozen=True)
class GardenlinuxFlavour:
    '''
    A specific flavour of gardenlinux.
    '''
    architecture: Architecture
    platform: str
    modifiers: typing.Tuple[Modifier, ...]

    def calculate_modifiers(self):
        yield from (
            feature_by_name(f) for f
            in self.modifiers
        )

    def canonical_name_prefix(self):
        a = self.architecture.value
        fname_prefix = self.filename_prefix()

        return f'{a}/{fname_prefix}'

    def filename_prefix(self):
        p = self.platform
        m = '_'.join(sorted([m for m in self.modifiers]))

        return f'{p}-{m}'

    def __post_init__(self):
        # validate platform and modifiers
        platform_names = {platform.name for platform in platforms()}
        if not self.platform in platform_names:
            raise ValueError(
                f'unknown platform: {self.platform}. known: {platform_names}'
            )

        modifier_names = {modifier.name for modifier in modifiers()}
        unknown_mods = set(self.modifiers) - modifier_names
        if unknown_mods:
            raise ValueError(
                f'unknown modifiers: {unknown_mods}. known: {modifier_names}'
            )


@dataclasses.dataclass(frozen=True)
class GardenlinuxFlavourCombination:
    '''
    A declaration of a set of gardenlinux flavours. Deserialised from `flavours.yaml`.

    We intend to build a two-digit number of gardenlinux flavours (combinations
    of different architectures, platforms, and modifiers). To avoid tedious and redundant
    manual configuration, flavourset combinations are declared. Subsequently, the
    cross product of said combinations are generated.
    '''
    architectures: typing.Tuple[Architecture, ...]
    platforms: typing.Tuple[Platform, ...]
    modifiers: typing.Tuple[typing.Tuple[Modifier, ...], ...]


@dataclasses.dataclass(frozen=True)
class GardenlinuxFlavourSet:
    '''
    A set of gardenlinux flavours
    '''
    name: str
    flavour_combinations: typing.Tuple[GardenlinuxFlavourCombination, ...]

    def flavours(self):
        for comb in self.flavour_combinations:
            for arch, platf, mods in itertools.product(
                comb.architectures,
                comb.platforms,
                comb.modifiers,
            ):
                yield GardenlinuxFlavour(
                    architecture=arch,
                    platform=platf,
                    modifiers=mods,
                )


@dataclasses.dataclass(frozen=True)
class ReleaseFile:
    '''
    base class for release-files
    '''
    name: str
    suffix: str
    md5sum: typing.Optional[str]
    sha256sum: typing.Optional[str]


@dataclasses.dataclass(frozen=True)
class S3_ReleaseFile(ReleaseFile):
    '''
    A single build result file that was (or will be) uploaded to build result persistency store
    (S3).
    '''
    s3_key: str
    s3_bucket_name: str


@dataclasses.dataclass(frozen=True)
class ReleaseIdentifier:
    '''
    a partial ReleaseManifest with all attributes required to unambiguosly identify a
    release.
    '''
    build_committish: str
    version: str
    gardenlinux_epoch: int
    architecture: Architecture
    platform: Platform
    modifiers: typing.Tuple[Modifier, ...]

    def flavour(self, normalise=True) -> GardenlinuxFlavour:
        mods = normalised_modifiers(
            platform=self.platform, modifiers=self.modifiers)

        return GardenlinuxFlavour(
            architecture=self.architecture,
            platform=self.platform,
            modifiers=self.modifiers,
        )

    def canonical_release_manifest_key_suffix(self):
        '''
        returns the canonical release manifest key. This key is used as a means to
        unambiguously identify it, and to thus be able to calculate its name if checking
        whether or not the given gardenlinux flavour has already been built and published.

        the key consists of:

        <canonical flavour name>-<version>-<commit-hash[:7]>

        where <canonical flavour name> is calculated from canonicalised_features()
        and <version> is the intended target release version.

        note that the full key should be prefixed (e.g. with manifest_key_prefix)
        '''
        cname = canonical_name(platform=self.platform, modifiers=self.modifiers, architecture=self.architecture, version=self.version)
        return f'{cname}-{self.build_committish[:8]}'

    def canonical_release_manifest_key(self):
        return f'{self.manifest_key_prefix}/{self.canonical_release_manifest_key_suffix()}'

    # attrs below are _transient_ (no typehint) and thus exempted from x-serialisation
    # treat as "static final"
    manifest_key_prefix = 'meta/singles'


class PublishedImageBase:
    pass


@dataclasses.dataclass(frozen=True)
class AwsPublishedImage:
    ami_id: str
    aws_region_id: str
    image_name: str


@dataclasses.dataclass(frozen=True)
class AwsPublishedImageSet(PublishedImageBase):
    published_aws_images: typing.Tuple[AwsPublishedImage, ...]
    # release_identifier: typing.Optional[ReleaseIdentifier]


@dataclasses.dataclass(frozen=True)
class AlicloudPublishedImage:
    image_id: str
    region_id: str
    image_name: str


@dataclasses.dataclass(frozen=True)
class AlicloudPublishedImageSet(PublishedImageBase):
    published_alicloud_images: typing.Tuple[AlicloudPublishedImage, ...]


@dataclasses.dataclass(frozen=True)
class GcpPublishedImage(PublishedImageBase):
    gcp_image_name: str
    gcp_project_name: str


class AzureTransportState(enum.Enum):
    PROVISIONAL = 'provisional'
    PUBLISH = 'publishing'
    GO_LIVE = 'going_live'
    RELEASED = 'released'
    FAILED = 'failed'


class AzureHyperVGeneration(enum.Enum):
    V1 = 'V1'
    V2 = 'V2'


class AzureCloud(enum.Enum):
    PUBLIC = 'public'
    CHINA = 'china'

    def authority(self):
        authorities = {
            AzureCloud.PUBLIC: "login.microsoftonline.com",
            AzureCloud.CHINA: "login.chinacloudapi.cn"
        }
        return authorities.get(self)

    def credential_scope(self):
        credential_scopes = {
            AzureCloud.PUBLIC: "https://management.azure.com/.default",
            AzureCloud.CHINA: "https://management.chinacloudapi.cn/.default"
        }
        return credential_scopes.get(self)

    def base_url(self):
        base_urls = {
            AzureCloud.PUBLIC: "https://management.azure.com",
            AzureCloud.CHINA: "https://management.chinacloudapi.cn",
        }
        return base_urls.get(self)

    def storage_endpoint(self):
        storage_endpoints = {
            AzureCloud.PUBLIC: "core.windows.net",
            AzureCloud.CHINA: "core.chinacloudapi.cn",
        }
        return storage_endpoints.get(self)


@dataclasses.dataclass(frozen=True)
class AzurePublishedImage:
    published_marketplace_images: typing.List[AzureMarketplacePublishedImage]
    published_gallery_images: typing.List[AzureImageGalleryPublishedImage]


@dataclasses.dataclass(frozen=True)
class AzureImageGalleryPublishedImage:
    '''
    AzureImageGalleryPublishedImage holds information about images that were
    published to Azure Community Image Galleries.
    '''
    hyper_v_generation: str
    community_gallery_image_id: typing.Optional[str]
    azure_cloud: typing.Optional[str] = AzureCloud.PUBLIC.value


@dataclasses.dataclass(frozen=True)
class AzureMarketplacePublishedImage:
    '''
    AzureMarketplacePublishedImage hold information about the publishing process of an image
    to the Azure Marketplace.

    urn is the image identfier used to spawn virtual machines with the image.

    publish_operation_id is the id of the publish operation of the image to the
    Azure Marketplace. At the end of this process step the image is validated and
    can be used for user their subscription get whitelisted.

    golive_operation_id is the id of the go live/release operation of the image
    to the Azure Marketplace. At the end of this process step the image is available
    in all Azure regions for general usage.
    '''
    hyper_v_generation: AzureHyperVGeneration
    publish_operation_id: str
    golive_operation_id: str
    urn: str


@dataclasses.dataclass(frozen=True)
class OpenstackPublishedImage:
    region_name: str
    image_id: str
    image_name: str


@dataclasses.dataclass(frozen=True)
class OpenstackPublishedImageSet(PublishedImageBase):
    published_openstack_images: typing.Tuple[OpenstackPublishedImage, ...]


@dataclasses.dataclass(frozen=True)
class OciPublishedImage:
    image_reference: str


class TestResultCode(enum.Enum):
    OK = 'success'
    FAILED = 'failure'


@dataclasses.dataclass(frozen=True)
class ReleaseTestResult:
    test_suite_cfg_name: str
    test_result: TestResultCode
    test_timestamp: str


@dataclasses.dataclass(frozen=True)
class ReleaseManifest(ReleaseIdentifier):
    '''
    metadata for a gardenlinux release variant that can be (or was) published to a persistency
    store, such as an S3 bucket.
    '''
    build_timestamp: str
    paths: typing.Tuple[S3_ReleaseFile, ...]
    base_image: typing.Optional[str]


    published_image_metadata: typing.Union[
        AlicloudPublishedImageSet,
        AwsPublishedImageSet,
        AzurePublishedImage,
        GcpPublishedImage,
        OciPublishedImage,
        OpenstackPublishedImageSet,
        None,
    ]

    def path_by_suffix(self, suffix: str):
        for path in self.paths:
            if path.suffix == suffix:
                return path
        else:
            raise ValueError(f'no path with {suffix=} in {self=}')

    def release_identifier(self) -> ReleaseIdentifier:
        return ReleaseIdentifier(
            build_committish=self.build_committish,
            version=self.version,
            gardenlinux_epoch=int(self.gardenlinux_epoch),
            architecture=self.architecture,
            platform=self.platform,
            modifiers=self.modifiers,
        )

    def build_ts_as_date(self) -> datetime.datetime:
        return dateutil.parser.isoparse(self.build_timestamp)


def normalised_release_identifier(release_identifier: ReleaseIdentifier):
    features = canonical_features(
        platform=release_identifier.platform,
        modifiers=release_identifier.modifiers,
        architecture=release_identifier.architecture.value,
        version=release_identifier.version
    )
    modifiers = ','.join(f.name for f in features)

    return dataclasses.replace(release_identifier, modifiers=modifiers)


def canonical_features(platform: Platform, modifiers, architecture, version) -> typing.Tuple[FeatureDescriptor]:
    '''
    calculates the "canonical" (/minimal) tuple of features required to unambiguosly identify
    a gardenlinux flavour. The result is returned as a (ASCII-upper-case-sorted) tuple of
    `FeatureDescriptor`, including the platform.

    The minimal featureset is determined by removing all transitive dependencies (which are thus
    implied by the retained features).
    '''
    feature_str = _garden_feat(platform=platform, modifiers=modifiers, arch=architecture, version=version, cmd='features')

    return tuple(
        feature_by_name(f)
        for f in feature_str.split(',')
    )


def canonical_name(
    platform: Platform,
    modifiers,
    version: str|None=None,
    architecture: Architecture|None=None,
) -> str:
    '''Calculates the canonical name of a gardenlinux flavour.

    The canonical name consists of the minimal sorted set of features in the given flavour, as
    determined by bin/garden-feat, with the platform always being the first element.
    '''
    cname = _garden_feat(platform=platform, modifiers=modifiers, arch=architecture.value, version=version)

    return cname


@dataclasses.dataclass(frozen=True)
class OnlineReleaseManifest(ReleaseManifest):
    '''
    a `ReleaseManifest` that was uploaded to a S3 bucket
    '''
    # injected iff retrieved from s3 bucket
    s3_key: str
    s3_bucket: str
    test_result: typing.Optional[ReleaseTestResult]
    logs: typing.Optional[typing.Union[S3_ReleaseFile, str]] = None
            # Note Union can be removed after all old manifests have been removed

    def stripped_manifest(self):
        raw = dataclasses.asdict(self)
        del raw['s3_key']
        del raw['s3_bucket']

        return ReleaseManifest(**raw)

    @classmethod
    def from_release_manifest(
        cls,
        release_manifest: ReleaseManifest,
        test_result: ReleaseTestResult,
    ):
        return OnlineReleaseManifest(
            **release_manifest.__dict__,
            test_result=test_result
        )

    def with_test_result(self,  test_result: ReleaseTestResult):
        return dataclasses.replace(self, test_result=test_result)

    def with_logfile(self, log: S3_ReleaseFile):
        return dataclasses.replace(self, logs=log)



@dataclasses.dataclass(frozen=True)
class ReleaseManifestSet:
    manifests: typing.Tuple[OnlineReleaseManifest, ...]
    flavour_set_name: str

    # treat as static final
    release_manifest_set_prefix = 'meta/sets'


@dataclasses.dataclass(frozen=True)
class OnlineReleaseManifestSet(ReleaseManifestSet):
    # injected iff retrieved from s3 bucket
    s3_key: str
    s3_bucket: str
    logs: typing.Optional[typing.Tuple[S3_ReleaseFile, ...]] = None

    def with_logfiles(self, files: typing.Tuple[S3_ReleaseFile]):
        log_files = self.logs
        if log_files:
            log_files = log_files + files
        else:
            log_files = files
        return dataclasses.replace(self, logs=log_files)


class PipelineFlavour(enum.Enum):
    SNAPSHOT = 'snapshot'
    RELEASE = 'release'


class BuildType(enum.Enum):
    SNAPSHOT = 'snapshot'
    DAILY = 'daily'
    RELEASE = 'release'


@dataclasses.dataclass(frozen=True)
class BuildCfg:
    aws_cfg_name: str
    aws_region: str
    s3_bucket_name: str
    gcp_bucket_name: str
    gcp_cfg_name: str
    storage_account_config_name: str
    service_principal_name: str
    plan_config_name: str
    oss_bucket_name: str
    alicloud_region: str
    alicloud_cfg_name: str


@dataclasses.dataclass(frozen=True)
class AwsPublishCfg:
    aws_cfg_names: typing.Tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class PackageBuildCfg:
    aws_cfg_name: str
    s3_bucket_name: str


@dataclasses.dataclass(frozen=True)
class AzureMarketplaceCfg:
    offer_id: str
    publisher_id: str
    plan_id: str
    notification_emails: list[str]


@dataclasses.dataclass(frozen=True)
class AzureServicePrincipalCfg:
    tenant_id: str
    client_id: str
    client_secret: str
    subscription_id: str


@dataclasses.dataclass(frozen=True)
class AzureStorageAccountCfg:
    storage_account_name: str
    container_name: str
    container_name_sig: str
    access_key: str
    endpoint_suffix: str


@dataclasses.dataclass(frozen=True)
class AzurePublishCfg:
    offer_id: str
    publisher_id: str
    plan_id: str
    service_principal_cfg_name: str
    storage_account_cfg_name: str
    shared_gallery_cfg_name: typing.Optional[str]
    notification_emails: typing.Tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class AzureSharedGalleryCfg:
    resource_group_name: str
    gallery_name: str
    location: str
    published_name: str
    description: str
    eula: str
    release_note_uri: str
    identifier_publisher: str
    identifier_offer: str
    identifier_sku: str
    regions: tuple[str]

@dataclasses.dataclass(frozen=True)
class OpenstackEnvironment:
    auth_url: str
    domain: str
    region: str
    project_name: str
    username: str
    password: str


@dataclasses.dataclass(frozen=True)
class OpenstackPublishCfg:
    environment_cfg_name: str
    image_properties_cfg_name: str


@dataclasses.dataclass(frozen=True)
class OciPublishCfg:
    image_prefix: str


@dataclasses.dataclass(frozen=True)
class CiPublishCfg:
    committish: str
    epoch: int
    version: str


@dataclasses.dataclass(frozen=True)
class PublishCfg:
    aws: AwsPublishCfg
    azure: AzurePublishCfg
    ci: CiPublishCfg
    oci: OciPublishCfg
    openstack: OpenstackPublishCfg


@dataclasses.dataclass(frozen=True)
class NotificationCfg:
    email_cfg_name: str
    slack_cfg_name: str
    slack_channel: str
    branches: typing.Tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class CicdCfg:
    name: str
    build: BuildCfg
    publish: PublishCfg
    notify: NotificationCfg
    package_build: typing.Optional[PackageBuildCfg]


class BuildResultBucketRole(enum.Enum):
    SOURCE = 'source'
    REPLICA = 'replica'


class ManifestBucketRole(enum.Enum):
    SOURCE = 'source'
    TARGET = 'target'


@dataclasses.dataclass
class ManifestS3Bucket:
    name: str
    role: ManifestBucketRole
    bucket_name: str
    aws_cfg_name: str


@dataclasses.dataclass
class BuildresultS3Bucket:
    name: str
    role: BuildResultBucketRole
    bucket_name: str
    aws_cfg_name: str
    platforms: list[Platform] = None


@dataclasses.dataclass
class PublishingTargetOci:
    image_prefix: str
    platform: Platform = 'oci' # should not overwrite


@dataclasses.dataclass
class PublishingTargetAliyun:
    aliyun_cfg_name: str
    oss_bucket_name: str
    aliyun_region: str
    copy_regions: typing.Optional[list[str]]
    platform: Platform = 'ali' # should not overwrite


@dataclasses.dataclass
class PublishingTargetAWSAccount:
    aws_cfg_name: str
    buildresult_bucket: str
    copy_regions: typing.Optional[list[str]]


@dataclasses.dataclass
class PublishingTargetAWS:
    aws_cfgs: list[PublishingTargetAWSAccount]
    image_tags: typing.Optional[ImageTagConfiguration]
    platform: Platform = 'aws' # should not overwrite


@dataclasses.dataclass
class PublishingTargetGCP:
    gcp_cfg_name: str
    gcp_bucket_name: str
    platform: Platform = 'gcp' # should not overwrite


@dataclasses.dataclass
class PublishingTargetAzure:
    cloud: AzureCloud
    buildresult_bucket: typing.Optional[str]
    gallery_cfg_name: str
    storage_account_cfg_name: str
    service_principal_cfg_name: str
    marketplace_cfg: typing.Optional[AzureMarketplaceCfg]
    hyper_v_generations: typing.List[AzureHyperVGeneration]
    publish_to_marketplace: bool
    publish_to_community_galleries: bool
    gallery_regions: typing.Optional[list[str]]
    platform: Platform = 'azure' # should not overwrite


@dataclasses.dataclass
class PublishingTargetOpenstack:
    environment_cfg_name: str
    image_properties: typing.Optional[dict[str, str]]
    suffix: typing.Optional[str]
    copy_regions: typing.Optional[list[str]]
    cn_regions: typing.Optional[OpenstackChinaRegions]
    platform: Platform = 'openstack' # should not overwrite

@dataclasses.dataclass
class OpenstackChinaRegions:
    region_names: typing.List[str]
    buildresult_bucket: str

@dataclasses.dataclass
class PublishingTargetOpenstackBareMetal(PublishingTargetOpenstack):
    platform: Platform = 'openstackbaremetal' # should not overwrite


@dataclasses.dataclass
class OpenStackImageProperties:
    hypervisor_type: str
    openstack_properties: dict[str, str]

@dataclasses.dataclass
class ImageTagConfiguration:
    include_gardenlinux_version: typing.Optional[bool]
    include_gardenlinux_committish: typing.Optional[bool]
    static_tags: typing.Optional[dict[str, str]]


@dataclasses.dataclass
class OcmCfg:
    ocm_repository: str
    overwrite_component_descriptor: typing.Optional[bool]

@dataclasses.dataclass
class S3_ManifestVersion:
    epoch: str
    version: str
    committish: str

@dataclasses.dataclass
class S3_Manifest(S3_ManifestVersion):
    manifest_key: str

@dataclasses.dataclass
class PublishingCfg:
    name: str
    manifest_s3_buckets: list[ManifestS3Bucket]
    buildresult_s3_buckets: list[BuildresultS3Bucket]
    ocm: OcmCfg
    targets: list[
        typing.Union[
            PublishingTargetAliyun,
            PublishingTargetAWS,
            PublishingTargetGCP,
            PublishingTargetAzure,
            PublishingTargetOpenstack,
        ],
        ...
    ]

    def target(self, platform: Platform, absent_ok=False):
        for t in self.targets:
            if t.platform == platform:
                return t

        if absent_ok:
            return None

        raise ValueError(f'no cfg for {platform=}')

    def target_multi(self, platform: Platform, absent_ok=False):
        target_list = [
            t for t in self.targets if t.platform == platform
        ]

        if len(target_list) > 0:
            return target_list

        if absent_ok:
            return None

        raise ValueError(f'no cfgs for {platform=}')

    def buildresult_bucket(self, name: str):
        for bucket in self.buildresult_s3_buckets:
            if bucket.name == name:
                return bucket
        raise ValueError(f'no buildresult-bucket {name=}')

    @property
    def origin_buildresult_bucket(self) -> BuildresultS3Bucket:
        for bucket in self.buildresult_s3_buckets:
            if bucket.role is BuildResultBucketRole.SOURCE:
                return bucket
        raise RuntimeError('did not find buildresult-bucket w/ role `source`')

    @property
    def replica_buildresult_buckets(self) -> typing.Generator[BuildresultS3Bucket, None, None]:
        for bucket in self.buildresult_s3_buckets:
            if bucket.role is BuildResultBucketRole.REPLICA:
                yield bucket

    @property
    def source_manifest_bucket(self) -> ManifestS3Bucket:
        for bucket in self.manifest_s3_buckets:
            if bucket.role is ManifestBucketRole.SOURCE:
                return bucket
        raise RuntimeError('did not find manifest-bucket w/ role `source`')

    @property
    def target_manifest_buckets(self) -> typing.Generator[BuildresultS3Bucket, None, None]:
        for bucket in self.manifest_s3_buckets:
            if bucket.role is ManifestBucketRole.TARGET:
                yield bucket


@dataclasses.dataclass
class PublishingVersion:
    name: str
    version: str
    commit: str


epoch_date = datetime.datetime.fromisoformat('2020-04-01')

# special version value - use "today" as gardenlinux epoch (depend on build-time)
version_today = 'today'
version_dev = 'dev'


def gardenlinux_epoch(date: typing.Union[str, datetime.datetime] = None):
    '''
    calculates the gardenlinux epoch for the given date (the amount of days since 2020-04-01)
    @param date: date (defaults to today); if str, must be compliant to iso-8601
    '''
    if date is None:
        date = datetime.datetime.today()
    elif isinstance(date, str):
        date = dateutil.parser.isoparse(date)

    if not isinstance(date, datetime.datetime):
        raise ValueError(date)

    gardenlinux_epoch = (date - epoch_date).days + 1

    if gardenlinux_epoch < 1:
        raise ValueError()  # must not be older than gardenlinux' inception
    return gardenlinux_epoch


_gl_epoch = gardenlinux_epoch  # alias for usage in snapshot_date


def snapshot_date(gardenlinux_epoch: int = None):
    '''
    calculates the debian snapshot repository timestamp from the given gardenlinux epoch in the
    format that is expected for said snapshot repository.
    @param gardenlinux_epoch: int, the gardenlinux epoch
    '''
    if gardenlinux_epoch is None:
        gardenlinux_epoch = _gl_epoch()
    gardenlinux_epoch = int(gardenlinux_epoch)
    if gardenlinux_epoch < 1:
        raise ValueError(gardenlinux_epoch)

    time_d = datetime.timedelta(days=gardenlinux_epoch - 1)

    date_str = (epoch_date + time_d).strftime('%Y%m%d')
    return date_str


def _parse_version_from_workingtree(version_file_path: str=paths.version_path) -> str:
    '''
    parses the raw version as configured (defaulting to the contents of `VERSION` file)

    In particular, the contents of `VERSION` (a regular text file) are parsed, with the following
    semantics:

    - lines are stripped
    - after stripping, lines starting with `#` are ignored
    - the first non-empty line (after stripping and comment-stripping) is considered
    - extra lines are ignored
    - from this line, trailing comments are removed (with another subsequent strip)
    - the result is then expected to be one of:
      - a semver-ish version (<major>.<minor>)
      - the string literal `today`
    - the afforementioned assumptions about the version are, however, not validated by this function
    '''
    with open(version_file_path) as f:
        for line in f.readlines():
            if not (line := line.strip()) or line.startswith('#'): continue
            version_str = line
            if '#' in version_str:
                # ignore comments
                version_str = version_str.split('#', 1)[0].strip()
            return version_str
        else:
            raise ValueError(f'did not find uncommented, non-empty line in {version_file_path}')


def next_release_version_from_workingtree(
    epoch: str=gardenlinux_epoch(),
    version_file_path: str=paths.version_path
):
    version_str = _parse_version_from_workingtree(version_file_path=version_file_path)

    if version_str == version_today or version_str == version_dev:
        # the first release-candidate is always <gardenlinux-epoch>.0
        version = f'{gardenlinux_epoch_from_workingtree(epoch = epoch)}.0'
        if version_str == version_dev:
            return version, version_dev
        return version, version

    # if version is not `today`, we expect to period-separated integers (<epoch>.<patchlevel>)
    epoch, patchlevel = version_str.split('.')

    # ensure the components are both parsable to int
    int(epoch)
    int(patchlevel)

    return version_str, version_str


def gardenlinux_epoch_from_workingtree(
    version_file_path: str=paths.version_path,
    epoch: str=gardenlinux_epoch()
):
    '''
    determines the configured gardenlinux epoch from the current working tree.

    see `_parse_version_from_workingtree` for details about pre-parsing / comment-stripping

    - the version_str is expected to be one of:
      - a semver-ish version (<major>.<minor>)
        - only <major> is considered (and must be parsable to an integer
        - the parsing result is the gardenlinux epoch
      - the string literal `today`
        - in this case, the returned epoch is today's gardenlinux epoch (days since 2020-04-01)
    '''
    version_str = _parse_version_from_workingtree(version_file_path=version_file_path)

    # version_str may either be a semver-ish (gardenlinux only uses two components (x.y))
    try:
        epoch = int(version_str.split('.')[0])
        return epoch
    except ValueError:
        pass

    if version_str == version_today or version_str == version_dev:
        return epoch

    raise ValueError(f'{version_str=} was not understood - either semver, "dev" or "today" are supported')


def _enumerate_feature_files(features_dir=os.path.join(paths.gardenlinux_dir, 'features')):
    for root, _, files in os.walk(features_dir):
        for name in files:
            if not name == 'info.yaml':
                continue
            yield os.path.join(root, name)


def _deserialise_feature(feature_file):
    with open(feature_file) as f:
        parsed = yaml.safe_load(f)
    # hack: inject name from pardir
    pardir = os.path.basename(os.path.dirname(feature_file))
    parsed['name'] = pardir

    # HACK HACK HACK: patch flags and features back to just `modifiers`
    if parsed['type'] in ('element', 'flag'):
        parsed['type'] = 'modifier'

    return dacite.from_dict(
        data_class=FeatureDescriptor,
        data=parsed,
        config=dacite.Config(
            cast=[
                FeatureType,
                tuple,
            ],
        ),
    )


@functools.lru_cache
def features():
    return {
        _deserialise_feature(feature_file)
        for feature_file in _enumerate_feature_files()
    }


def platforms():
    return {
        feature for feature in features() if feature.type is FeatureType.PLATFORM
    }


def platform_names():
    return {p.name for p in platforms()}


def modifiers():
    return {
        # HACK: including metal here is a terrible hack to get the openstackbaremetal flavour out
        # this needs to be fixed on Garden Linux side by making metal a modifier, not a platform
        feature for feature in features() if feature.type is FeatureType.MODIFIER or feature.name == 'metal'
    }


def feature_by_name(feature_name: str):
    for feature in features():
        if feature.name == feature_name:
            return feature
    raise ValueError(feature_name)


def _garden_feat(
    platform: str,
    modifiers: typing.Tuple[str, ...],
    arch: str|None,
    version: str|None,
    cmd: str = 'cname',
) -> str:
    if not version or not arch:
        cmd = "cname_base"

    all_mods = set(tuple(modifiers) + (platform,))
    feature_binary = os.path.abspath(os.path.join(paths.gardenlinux_builder_dir, 'builder', 'parse_features'))
    feature_args=[
            feature_binary,
            '--feature-dir', os.path.abspath(os.path.join(paths.gardenlinux_dir, 'features')),
            '--features', ','.join(all_mods),
    ]

    if arch:
        feature_args.extend(["--arch", arch])
    
    if version:
        feature_args.extend(["--version", version])

    feature_args.append(cmd)

    parse_features_proc = None
    try:
        parse_features_proc = subprocess.run(
            args=feature_args,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as parse_features_exception:
        print(f"{parse_features_exception.stdout=}\n{parse_features_exception.stderr=}\n{parse_features_exception.returncode}")
        raise parse_features_exception

    return parse_features_proc.stdout.strip()

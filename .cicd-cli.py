#!/usr/bin/env python3

import argparse
import dataclasses
import enum
import git
import io
import logging
import os
import pprint
import re
import sys
import yaml

import component_descriptor as cd
import promote
import replicate
import product.v2

import ccc.aws
import ctx

logger = logging.getLogger('gardenlinux-cli')

own_dir = os.path.abspath(os.path.dirname(__file__))
ci_dir = os.path.join(own_dir, 'ci')

sys.path.insert(1, ci_dir)

import clean      # noqa: E402
import glci.util  # noqa: E402
import glci.model # noqa: E402
import paths      # noqa: E402


# see also:
# https://stackoverflow.com/questions/43968006/support-for-enum-arguments-in-argparse/55500795
class EnumAction(argparse.Action):
    """
    Argparse action for handling Enums
    """
    def __init__(self, **kwargs):
        # Pop off the type value
        enum_type = kwargs.pop("type", None)

        # Ensure an Enum subclass is provided
        if enum_type is None:
            raise ValueError("type must be assigned an Enum when using EnumAction")
        if not issubclass(enum_type, enum.Enum):
            raise TypeError("type must be an Enum when using EnumAction")

        # Generate choices from the Enum
        kwargs.setdefault("choices", tuple(e.value for e in enum_type))

        super(EnumAction, self).__init__(**kwargs)

        self._enum = enum_type

    def __call__(self, parser, namespace, values, option_string=None):
        # Convert value back into an Enum
        value = self._enum(values)
        setattr(namespace, self.dest, value)


def clean_build_result_repository():
    parser = argparse.ArgumentParser(
        description='Cleanup in manifests repository (S3)',
        epilog='Warning: dangerous, use only if you know what you are doing!',
    )
    parser.add_argument(
        '--cicd-cfg',
        default='default',
        help='configuration key for ci, default: \'%(default)s\'',
        )
    parser.add_argument(
        '--snapshot-max-age-days',
        default=30,
        help='delete manifests older than (number of days), default: %(default)s',
        type=int,
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help=('only print information about objects to be deleted'),
    )

    parsed = parser.parse_args()

    cicd_cfg = glci.util.cicd_cfg(parsed.cicd_cfg)

    print('purging outdated build snapshot manifests')
    clean.clean_single_release_manifests(
        max_age_days=parsed.snapshot_max_age_days,
        cicd_cfg=cicd_cfg,
        dry_run=parsed.dry_run,
    )

    print('purging outdated build result snapshot sets (release-candidates)')
    clean.clean_release_manifest_sets(
        max_age_days=parsed.snapshot_max_age_days,
        cicd_cfg=cicd_cfg,
        dry_run=parsed.dry_run,
    )

    print('purging loose objects')
    clean.clean_orphaned_objects(
        cicd_cfg=cicd_cfg,
        dry_run=parsed.dry_run,
    )


def gardenlinux_epoch():
    print(glci.model.gardenlinux_epoch_from_workingtree())


def gardenlinux_timestamp():
    epoch = glci.model.gardenlinux_epoch_from_workingtree()

    print(glci.model.snapshot_date(gardenlinux_epoch=epoch))


def _gitrepo():
    repo = git.Repo(paths.repo_root)
    return repo


def _head_sha():
    repo = _gitrepo()
    return repo.head.commit.hexsha


def  _fix_version(parsed_version: str, parsed_epoch: int):
    """
    Check if parsed version is a semver version number and issue a warning if not
    if argument default is used and it is semver it is likely 'today'. Use
    current day in this case.
    """
    pattern = re.compile(r'^[\d\.]+$')
    is_proper_version = pattern.match(parsed_version)
    # check if default is used from argparser
    if parsed_version != glci.model._parse_version_from_workingtree():
        if not is_proper_version:
            print(f'>>> WARNING: {parsed_version} is not a semver version! <<<')
        result = parsed_version
    else:
        if is_proper_version:
            result = parsed_version
        else:
            result = f'{parsed_epoch}.0'

    if parsed_epoch != int(result.split('.')[0]):
        print(f'>>> WARNING: version {result} does not match epoch {parsed_epoch}! <<<')
    return result


def _download_obj_to_file(
    cicd_cfg: glci.util.cicd_cfg,
    bucket_name: str,
    s3_key: str,
    file_name: str,
):
    s3_session = ccc.aws.session(cicd_cfg.build.aws_cfg_name)
    s3_client = s3_session.client('s3')
    s3_client.download_file(bucket_name, s3_key, file_name)
    return 0


def _download_release_artifact(
        cicd_cfg: glci.util.cicd_cfg,
        name: str,
        outfile: str,
        manifest: glci.model.OnlineReleaseManifest,
):
    if name == 'log' or name == 'logs':
        log_obj = manifest.logs
        if not log_obj:
            print('Error: No logs attached to release manifest')
            return 1
        elif type(log_obj) is glci.model.S3_ReleaseFile:
            s3_key = log_obj.s3_key
            s3_bucket = log_obj.s3_bucket_name
        else:
            s3_bucket = cicd_cfg.build.s3_bucket_name
            s3_key = log_obj # old format (str) can be removed if all old manifests are cleaned

    else:
        file_objs = [entry for entry in manifest.paths if entry.name == name]
        if not file_objs:
            print(f'Error: No object in release manifest with name {name}')
            return 1
        if len(file_objs) > 1:
            print(f'Warning.: Found more than one file with name {name}, using first one')
        s3_key = file_objs[0].s3_key
        s3_bucket = file_objs[0].s3_bucket_name

    print(f'Downloading object with S3-key: {s3_key} from bucket {s3_bucket}, to {outfile}')
    return _download_obj_to_file(
        cicd_cfg=cicd_cfg,
        bucket_name=s3_bucket,
        s3_key=s3_key,
        file_name=outfile,
    )


def _print_used_args(parsed_args: dict):
    print('finding release(set)s with following properties:')
    for arg_key, arg_value in parsed_args.items():
        if isinstance(arg_value, enum.Enum):
            arg_value = arg_value.value
        elif isinstance(arg_value, io.IOBase):
            arg_value = arg_value.name
        print(f'{arg_key} : {arg_value}')
    print('--------')


def _retrieve_argparse(parser):
    repo = _gitrepo()
    parser.add_argument(
        '--committish', '-c',
        default=_head_sha(),
        type=lambda c: repo.git.rev_parse(c),
        help='commit of this artifact (min. first 6 chars), default: HEAD',
    )
    parser.add_argument(
        '--cicd-cfg',
        default='default',
        help='configuration key for ci, default: \'%(default)s\'',
        )
    parser.add_argument(
        '--version',
        default=glci.model._parse_version_from_workingtree(),
        help='Gardenlinux version number, e.g. \'318.9\', default: %(default)s',
    )
    parser.add_argument(
        '--gardenlinux-epoch',
        default=glci.model.gardenlinux_epoch_from_workingtree(),
        help='Gardenlinux epoch, e.g. \'318\', default: %(default)s',
        type=int,
    )
    parser.add_argument(
        '--outfile', '-o',
        type=lambda f: open(f, 'w'),
        default=sys.stdout,
        help='destination file for output, default: stdout'
    )

    return parser


def retrieve_single_manifest():
    parser = argparse.ArgumentParser(
        description='Get manifests from the build artifact repository',
        epilog='Example: retrieve-single-manifest --architecture=amd64 --platform=aws '
        '--committish=71ceb0 --version=318.9 '
        '--gardenlinux-epoch=318 --modifier=_prod,gardener'
    )
    parser.add_argument(
        '--architecture',
        default=glci.model.Architecture.AMD64,
        type=glci.model.Architecture,
        action=EnumAction,
        help='CPU architecture, default: \'%(default)s\'',
    )
    parser.add_argument(
        '--platform',
        choices=[p.name for p in glci.model.platforms()],
        help='Target (virtualization) platform',
        required=True,
    )

    class AddModifierAction(argparse.Action):
        def __call__(self, parser, namespace, values, option_string):
            choices = [c.name for c in glci.model.modifiers()]

            raw_modifiers = []
            for v in values.split(','):
                if not (v := v.strip()) in choices:
                    raise ValueError(f'{v} not in {choices}')
                raw_modifiers.append(v)

            normalised_modifiers = glci.model.normalised_modifiers(
                platform=namespace.platform,
                modifiers=raw_modifiers,
            )

            setattr(namespace, self.dest, normalised_modifiers)

    parser.add_argument(
        '--modifier',
        action=AddModifierAction,
        dest='modifiers',
        default=('base', 'cloud', 'gardener', 'server', '_nopkg', '_prod', '_readonly', '_slim'),
        help='Feature set, comma-separated, see '
            'https://github.com/gardenlinux/gardenlinux/tree/main/features for possible values, '
            'default: %(default)s',
    )

    parser.add_argument(
        '--download',
        help='Download an artifact from this manifest, value is one of paths/name or log'
    )

    _retrieve_argparse(parser=parser)

    parsed = parser.parse_args()
    parsed.version = _fix_version(parsed.version, parsed.gardenlinux_epoch)
    _print_used_args(vars(parsed))

    find_release = glci.util.preconfigured(
        func=glci.util.find_release,
        cicd_cfg=glci.util.cicd_cfg(parsed.cicd_cfg)
    )

    release = find_release(
        release_identifier=glci.model.ReleaseIdentifier(
            build_committish=parsed.committish,
            version=parsed.version,
            gardenlinux_epoch=parsed.gardenlinux_epoch,
            architecture=parsed.architecture,
            platform=parsed.platform,
            modifiers=parsed.modifiers,
        )
    )

    if not release:
        print('ERROR: no such release found')
        sys.exit(1)

    if parsed.download:
        # try to download the given artifact
        if parsed.outfile == sys.stdout:
            if parsed.download == 'log' or parsed.download == 'logs':
                outfile_name = 'build_log.zip'
            else:
                outfile_name = parsed.download
        else:
            outfile_name = parsed.outfile.name
            parsed.outfile.close()
            if os.path.exists(outfile_name):
                os.remove(outfile_name)

        res_code = _download_release_artifact(
            cicd_cfg=glci.util.cicd_cfg(parsed.cicd_cfg),
            name=parsed.download,
            outfile=outfile_name,
            manifest=release,
        )
        return res_code

    with parsed.outfile as f:
        yaml.dump(
            data=dataclasses.asdict(release),
            stream=f,
            Dumper=glci.util.EnumValueYamlDumper,
        )


def retrieve_release_set():
    parser = argparse.ArgumentParser(
        description='Get manifest sets from the build artifact repository (S3)',
        epilog='Example: retrieve-release-set --version=27.1.0 --gardenlinux-epoch=27 --build-type=release' # noqa E501
    )
    _retrieve_argparse(parser=parser)
    parser.add_argument(
        '--flavourset-name',
        default='gardener',
        help='Flavour set, see: https://github.com/gardenlinux/gardenlinux/blob/main/flavours.yaml'
        ' default: %(default)s',
    )

    parser.add_argument(
        '--build-type',
        action=EnumAction,
        default=glci.model.BuildType.RELEASE,
        help='Build artifact type, default: \'%(default)s\'',
        type=glci.model.BuildType,
    )

    parsed = parser.parse_args()
    parsed.version = _fix_version(parsed.version, parsed.gardenlinux_epoch)
    _print_used_args(vars(parsed))

    find_release_set = glci.util.preconfigured(
        func=glci.util.find_release_set,
        cicd_cfg=glci.util.cicd_cfg(parsed.cicd_cfg),
    )

    release_set = find_release_set(
        flavour_set_name=parsed.flavourset_name,
        build_committish=parsed.committish,
        version=parsed.version,
        gardenlinux_epoch=parsed.gardenlinux_epoch,
        build_type=parsed.build_type,
        absent_ok=True,
    )

    if release_set is None:
        print('Did not find specified release-set')
        sys.exit(1)

    with parsed.outfile as f:
        yaml.dump(
            data=dataclasses.asdict(release_set),
            stream=f,
            Dumper=glci.util.EnumValueYamlDumper,
        )


def _add_flavourset_args(parser):
    parser.add_argument(
        '--flavourset-name',
        default='gardener',
    )
    parser.add_argument(
        '--flavours-file',
        default=None,
    )


def _flavourset(parsed):
    if parsed.flavours_file:
        flavours_path = parsed.flavours_file
    else:
        flavours_path = paths.flavour_cfg_path

    flavour_set = glci.util.flavour_set(
        flavour_set_name=parsed.flavourset_name,
        build_yaml=flavours_path,
    )

    return flavour_set


def _add_publishing_cfg_args(parser):
    parser.add_argument('--cfg-name', default='default')


def _publishing_cfg(parsed):
    cfg = glci.util.publishing_cfg(cfg_name=parsed.cfg_name)

    return cfg


def replicate_blobs():
    parser = argparse.ArgumentParser()
    _add_flavourset_args(parser)
    _add_publishing_cfg_args(parser)

    parser.add_argument(
        '--version',
    )
    parser.add_argument(
        '--commit',
    )

    parsed = parser.parse_args()

    cfg = _publishing_cfg(parsed)


    flavour_set = _flavourset(parsed)
    flavours = tuple(flavour_set.flavours())

    s3_session = ccc.aws.session(cfg.origin_buildresult_bucket.aws_cfg_name)
    s3_client = s3_session.client('s3')

    version = parsed.version

    cfg_factory = ctx.cfg_factory()

    release_manifests = tuple(
        glci.util.find_releases(
            s3_client=s3_client,
            bucket_name=cfg.origin_buildresult_bucket.bucket_name,
            flavour_set=flavour_set,
            build_committish=parsed.commit,
            version=version,
            gardenlinux_epoch=int(version.split('.')[0]),
        )
    )

    logger.info(f'found {len(release_manifests)=}')

    replicate.replicate_image_blobs(
        publishing_cfg=cfg,
        release_manifests=release_manifests,
        cfg_factory=cfg_factory,
    )


def ls_manifests():
    parser = argparse.ArgumentParser()

    _add_flavourset_args(parser)

    parser.add_argument(
        '--version-prefix',
        default=None,
        help='if given, filter for versions of given prefix',
    )
    parser.add_argument(
        '--print',
        default='all',
        choices=('all', 'versions', 'versions-and-commits'),
    )

    parsed = parser.parse_args()

    flavour_set = _flavourset(parsed)
    flavours = tuple(flavour_set.flavours())

    def iter_manifest_prefixes():
        key_prefix = glci.model.ReleaseIdentifier.manifest_key_prefix
        version_prefix = parsed.version_prefix

        for f in flavours:
            cname = glci.model.canonical_name(
                platform=f.platform,
                modifiers=f.modifiers,
                architecture=f.architecture,
            )
            prefix = f'{key_prefix}/{cname}'

            if version_prefix:
                prefix = f'{prefix}-{version_prefix}'

            yield prefix

    cfg = glci.util.cicd_cfg()
    s3_client = glci.s3.s3_client(cicd_cfg=cfg)

    versions = set()
    versions_and_commits = set()

    for prefix in iter_manifest_prefixes():
        matching_manifests = s3_client.list_objects_v2(
            Bucket=cfg.build.s3_bucket_name,
            Prefix=prefix,
        )
        for entry in matching_manifests['Contents']:
            key = entry['Key']
            if parsed.print == 'all':
                print(key)
            else:
                _, version, commit = key.rsplit('-', 2)
                if not version in versions:
                    versions.add(version)
                    if parsed.print == 'versions':
                        print(version)
                if not (version, commit) in versions_and_commits:
                    versions_and_commits.add((version, commit))
                    if parsed.print == 'versions-and-commits':
                        print(f'{version} {commit}')


def publish_release_set():
    parser = argparse.ArgumentParser(
        description='run all sub-steps for publishing gardenlinux to all target hyperscalers',
    )
    _add_flavourset_args(parser)
    _add_publishing_cfg_args(parser)

    phase_sync = 'sync-images'
    phase_publish = 'publish-images'
    phase_component_descriptor = 'publish-component-descriptor'

    parser.add_argument(
        '--version',
    )
    parser.add_argument(
        '--commit',
    )
    parser.add_argument(
        '--on-absent-cfg',
        choices=('warn', 'fail'),
        default='warn',
        help='behaviour upon absent publishing-cfg (see publishing-cfg.yaml)',
    )
    parser.add_argument(
        '--platform',
        action='append',
        dest='platforms',
        default=[],
        help='if set, only specified platforms will be published to (default: publish to all)',
    )
    parser.add_argument(
        '--force',
        action='store_true',
        default=False,
        help='publish images, even if already present according to release-manifests',
    )
    parser.add_argument(
        '--print-manifest',
        action='store_true',
        default=False,
    )
    parser.add_argument(
        '--print-component-descriptor',
        action='store_true',
        default=False,
    )
    parser.add_argument(
        '--phase',
        default=None,
        choices=(
            phase_sync,
            phase_publish,
            phase_component_descriptor,
        ),
        help='if set, only run until specified phase (default: run all)',
    )
    parser.add_argument(
        '--skip-previous-phases',
        action='store_true',
        default=False,
        help='if --phase is given, skip previous phases (for debugging purposes)',
    )

    parsed = parser.parse_args()

    cfg = _publishing_cfg(parsed)
    cfg_factory = ctx.cfg_factory()

    flavour_set = _flavourset(parsed)
    flavours = tuple(flavour_set.flavours())
    version = parsed.version
    commit = parsed.commit

    if len(commit) != 40:
        repo = git.Repo(path=paths.gardenlinux_dir)
        commit = repo.git.rev_parse(commit)
        logger.info(f'expanded commit to {commit}')


    logger.info(
        f'Publishing gardenlinux {version}@{commit} ({flavour_set.name=})\n'
    )

    logger.info(
        'phases to run:\n- ' + '\n- '.join((
            phase_sync,
            phase_publish,
            phase_component_descriptor,
        ))
    )
    print()

    def start_phase(name):
        logger = logging.getLogger(name)
        logger.info(20 * '=')
        logger.info(f'Starting Phase {name}')
        logger.info(20 * '=')
        print()
        return logger


    def end_phase(name):
        logger = logging.getLogger(name)
        logger.info(20 * '=')
        logger.info(f'End of Phase {name}')
        logger.info(20 * '=')
        print()
        if (phase := parsed.phase) and phase == name:
            logger.info(f'will stop here, as {name} was passed as final phase via ARGV')
            exit(0)

    phase_logger = start_phase('sync-images')

    s3_session = ccc.aws.session(cfg.origin_buildresult_bucket.aws_cfg_name)
    s3_client = s3_session.client('s3')

    origin_buildresult_bucket = cfg.origin_buildresult_bucket

    release_manifests = list(
        glci.util.find_releases(
            s3_client=s3_client,
            bucket_name=origin_buildresult_bucket.bucket_name,
            flavour_set=flavour_set,
            build_committish=commit,
            version=version,
            gardenlinux_epoch=int(version.split('.')[0]),
        )
    )

    if not release_manifests:
        phase_logger.fatal(
            f'did not find any release-manifests for {version=} {commit=}',
        )
        phase_logger.fatal(
            'hint: use `ls-manifests` command to find valid choices for version | commit'
        )
        exit(1)
    phase_logger.info(f'found {len(release_manifests)=}')

    if (skip_previous := parsed.skip_previous_phases) and (phase := parsed.phase):
        if phase == phase_sync:
            run_sync = True
        else:
            run_sync = False
    else:
        run_sync = True

    if run_sync:
        replicate.replicate_image_blobs(
            publishing_cfg=cfg,
            release_manifests=release_manifests,
            cfg_factory=cfg_factory,
        )
    else:
        phase_logger.info('skipping sync-images (--skip-previous-phases)')

    end_phase(phase_sync)

    phase_logger = start_phase(phase_publish)

    phase_logger.info('validating publishing-cfg')

    for manifest in release_manifests:
        target_cfg = cfg.target(platform=manifest.platform, absent_ok=True)
        if not target_cfg:
            if (on_absent := parsed.on_absent_cfg) == 'warn':
                phase_logger.warning(
                    f'no cfg for {manifest.platform=} - will NOT publish!'
                )
                continue
            elif on_absent == 'fail':
                phase_logger.fatal(
                    f'no cfg for {manifest.platform=} - aborting'
                )
            else:
                raise ValueError(ob_absent) # programming error

    phase_logger.info('publishing-cfg was found to be okay - starting publishing now')

    if skip_previous and phase:
        if phase == phase_publish:
            run_publish = True
        else:
            run_publish = False
    else:
        run_publish = False

    for idx, manifest in enumerate(release_manifests):
        if not run_publish:
            continue

        if parsed.platforms and not manifest.platform in parsed.platforms:
            logger.info(f'skipping {manifest.platform} (filter was set via ARGV)')
            continue

        name = manifest.release_identifier().canonical_release_manifest_key()
        phase_logger.info(name)

        target_cfg = cfg.target(platform=manifest.platform, absent_ok=True)
        if not target_cfg: # we already validated above that user is okay to skip
            continue

        if parsed.print_manifest:
            pprint.pprint(manifest)

        if manifest.published_image_metadata:
            if not parsed.force:
                phase_logger.info('already published -> skipping publishing phase')
                continue
            else:
                phase_logger.warning('force-publishing')

        phase_logger.info(f'will publish image to {manifest.platform}')

        updated_manifest = promote.publish_image(
            release=manifest,
            publishing_cfg=cfg,
        )
        release_manifests[idx] = updated_manifest

        target = f'{origin_buildresult_bucket.bucket_name}/{manifest.s3_key}'
        phase_logger.info(f'updating release-manifest at {target}')

        glci.util.upload_release_manifest(
            s3_client=s3_client,
            bucket_name=origin_buildresult_bucket.bucket_name,
            key=manifest.s3_key,
            manifest=updated_manifest,
        )

        phase_logger.info(f'image publishing for {manifest.platform} succeeded')

    if not run_publish:
        phase_logger.info('skipped image-publishing (--skip-previous-phases)')

    end_phase(phase_publish)

    phase_logger = start_phase(phase_component_descriptor)

    if parsed.platforms:
        phase_logger.error('must not filter platforms if publishing component-descriptor')
        phase_logger.error('component-descriptor is intended to always contain full release-set')
        exit(1)

    phase_logger.info('generating component-descriptor')
    component_descriptor = cd.component_descriptor(
        version=version,
        commit=commit,
        publishing_cfg=cfg,
        release_manifests=release_manifests,
        cfg_factory=cfg_factory,
    )

    if parsed.print_component_descriptor:
        pprint.pprint(component_descriptor)

    phase_logger.info('publishing component-descriptor')
    product.v2.upload_component_descriptor_v2_to_oci_registry(
        component_descriptor_v2=component_descriptor,
    )

    end_phase(phase_component_descriptor)


def main():
    cmd_name = os.path.basename(sys.argv[0]).replace('-', '_')

    module_symbols = sys.modules[__name__]

    func = getattr(module_symbols, cmd_name, None)

    if not func:
        print(f'ERROR: {cmd_name} is not defined')
        sys.exit(1)

    func()


if __name__ == '__main__':
    main()

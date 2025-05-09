import os

repo_root = os.path.abspath(os.path.dirname(__file__))
parent_dir = os.path.abspath(os.path.join(repo_root, os.path.pardir))

if os.environ.get('GARDENLINUX_PATH'):
    gardenlinux_dir = os.path.abspath(os.environ.get('GARDENLINUX_PATH'))
else:
    # hack: assume local user has a copy of gardenlinux-repo as sibling to this repo
    gardenlinux_dir = os.path.join(parent_dir, 'gardenlinux')

if not os.path.isdir(gardenlinux_dir):
    print(f'ERROR: expected worktree of gardenlinux repo at {gardenlinux_dir=}')
    exit(1)

if os.environ.get('GARDENLINUX_BUILDER_PATH'):
    gardenlinux_builder_dir = os.path.abspath(os.environ.get('GARDENLINUX_BUILDER_PATH'))
else:
    # hack: assume local user has a copy of gardenlinux-repo as sibling to this repo
    gardenlinux_builder_dir = os.path.join(parent_dir, 'gardenlinux-builder')

if not os.path.isdir(gardenlinux_builder_dir):
    print(f'ERROR: expected worktree of gardenlinux builder repo at {gardenlinux_builder_dir=}')
    exit(1)

publishing_cfg_path = os.path.join(repo_root, 'publishing-cfg.yaml')
publishing_versions_path = os.path.join(repo_root, 'publishing-versions.yaml')
package_alias_path = os.path.join(repo_root, 'package_aliases.yaml')

flavour_cfg_path = os.path.join(repo_root, 'flavours.yaml')

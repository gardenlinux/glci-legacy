import os

repo_root = os.path.abspath(os.path.dirname(__file__))
parent_dir = os.path.abspath(os.path.join(repo_root, os.path.pardir))

if os.environ.get('GARDENLINUX_PATH'):
    gardenlinux_dir = os.path.abspath(os.environ.get('GARDENLINUX_PATH'))
else:
    # hack: assume local user has a copy of gardenlinux-repo as sibling to this repo
    gardenlinux_dir = os.path.join(parent_dir, 'gardenlinux')
    gardenlinux_candidates = [gardenlinux_dir]

    # fallback for tekton-case
    if not os.path.isdir:
        gardenlinux_dir = os.path.abspath(os.path.join(parent_dir, 'gardenlinux_git'))
        gardenlinux_candidates.append(gardenlinux_dir)

if not os.listdir(gardenlinux_dir):
    print(f'ERROR: expected worktree of gardenlinux repo at {gardenlinux_candidates=}')
    exit(1)

cicd_cfg_path = os.path.join(repo_root, 'cicd.yaml')
package_alias_path = os.path.join(repo_root, 'package_aliases.yaml')

flavour_cfg_path = os.path.join(gardenlinux_dir, 'flavours.yaml')
version_path = os.path.join(repo_root, 'VERSION')

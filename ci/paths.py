import os

own_dir = os.path.abspath(os.path.dirname(__file__))
repo_root = os.path.abspath(os.path.join(own_dir, os.pardir))

# TODO: This will only work for default parameters. Consider setting env-vars during
# task render
workspace_dir = os.path.abspath(os.path.join(repo_root, os.path.pardir))
glci_dir = os.path.abspath(os.path.join(workspace_dir, 'glci_git'))

if os.environ.get('GARDENLINUX_PATH'):
    gardenlinux_dir = os.path.abspath(os.environ.get('GARDENLINUX_PATH'))
else:
    gardenlinux_dir = os.path.abspath(os.path.join(workspace_dir, 'gardenlinux_git'))

cicd_cfg_path = os.path.join(own_dir, 'cicd.yaml')
package_alias_path = os.path.join(own_dir, 'package_aliases.yaml')

flavour_cfg_path = os.path.join(repo_root, 'flavours.yaml')
version_path = os.path.join(repo_root, 'VERSION')

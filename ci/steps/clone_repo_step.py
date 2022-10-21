import base64
import logging
import os
import urllib.parse

import git

import ccc.github
import gitutil

logger = logging.getLogger(__name__)


def clone_and_checkout_with_technical_user(
    github_cfg,
    committish: str,
    repo_dir: str,
    repo_path: str,
):
    git_helper = gitutil.GitHelper.clone_into(
        target_directory=repo_dir,
        github_cfg=github_cfg,
        github_repo_path=repo_path,
    )
    repo = git_helper.repo

    repo.git.checkout(committish)

    return repo.head.commit.message, repo.head.commit.hexsha

def prepare_home_dir():
    if home_dir := os.environ.get('HOME'):
        logger.info(f"Preparing HOME at '{home_dir}'")
        os.makedirs(os.path.abspath(home_dir), exist_ok=True)


def clone_and_copy(
    giturl: str,
    committish: str,
    repo_dir: str,
    gardenlinux_giturl: str,
    gardenlinux_committish: str,
    gardenlinux_repo_dir: str,
):
    repo_dir = os.path.abspath(repo_dir)
    repo_url = urllib.parse.urlparse(giturl)

    gardenlinux_repo_dir = os.path.abspath(gardenlinux_repo_dir)
    gardenlinux_repo_url = urllib.parse.urlparse(gardenlinux_giturl)

    github_cfg = ccc.github.github_cfg_for_hostname(
        repo_url.hostname,
    )
    commit_msg, commit_hash = clone_and_checkout_with_technical_user(
        github_cfg=github_cfg,
        committish=committish,
        repo_dir=repo_dir,
        repo_path=repo_url.path,
    )

    gardenlinux_github_cfg = ccc.github.github_cfg_for_hostname(
        gardenlinux_repo_url.hostname,
    )
    gardenlinux_commit_msg, gardenlinux_commit_hash = clone_and_checkout_with_technical_user(
        github_cfg=gardenlinux_github_cfg,
        committish=gardenlinux_committish,
        repo_dir=gardenlinux_repo_dir,
        repo_path=gardenlinux_repo_url.path,
    )

    prepare_home_dir()

    logger.info(f'cloned {repo_url=} to {repo_dir=} {commit_hash=}')
    logger.info(f'Commit Message: {commit_msg}')

    logger.info(
        f'cloned {gardenlinux_repo_url=} to {gardenlinux_repo_dir=} {gardenlinux_commit_hash=}'
    )
    logger.info(f'Gardenlinux Commit Message: {gardenlinux_commit_msg}')

import logging
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List

import docker
import git
from docker.models.containers import Container

from repopal.schemas.command import CommandResult
from repopal.schemas.environment import EnvironmentConfig
from repopal.schemas.changes import (
    RepositoryChanges,
    TrackedChange,
    UntrackedChange,
)
from repopal.services.commands.base import Command
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    pass


class EnvironmentManager:
    """Manages Docker environments and Git repositories for command execution"""

    def __init__(self):
        self.docker_client = docker.from_env()
        self.work_dir: Optional[Path] = None
        self.container: Container | None = None
        self.logger = logging.getLogger(__name__)

    def setup_container(
        self, command: Command, environment: Dict[str, str] = None
    ) -> None:
        """Create and start a Docker container with the working directory mounted"""
        if not self.work_dir:
            raise ValueError(
                "Working directory not set up. Call git_repo_manager.clone_repo first."
            )

        # Create a temporary directory for the Dockerfile
        with tempfile.TemporaryDirectory() as docker_build_dir:
            dockerfile_path = Path(docker_build_dir) / "Dockerfile"
            dockerfile_path.write_text(command.dockerfile)

            # Build the image
            image, _ = self.docker_client.images.build(
                path=str(docker_build_dir), rm=True, forcerm=True
            )

            container_name = f"repopal-{command.metadata.name}"

            # Run the container
            self.container = self.docker_client.containers.run(
                image,
                name=container_name,
                detach=True,
                volumes={str(self.work_dir): {"bind": "/workspace", "mode": "rw"}},
                working_dir="/workspace",
                environment=environment or {},
                user="1000:1000",  # Run as non-root user
            )

    def get_repository_changes(self) -> RepositoryChanges:
        """Get the git diff of changes made in the repository

        Returns:
            RepositoryChanges containing tracked and untracked changes
        """
        if not self.work_dir:
            return RepositoryChanges(tracked_changes=[], untracked_changes=[])

        repo = git.Repo(self.work_dir)
        tracked_changes: List[TrackedChange] = []
        untracked_changes: List[UntrackedChange] = []
        
        # Get all changes including staged and unstaged
        if repo.is_dirty():
            # Get list of changed files
            changed_files = [item.a_path for item in repo.index.diff(None)] + [item.a_path for item in repo.index.diff('HEAD')]
            
            # Get diff for each changed file
            for file_path in set(changed_files):
                diff = repo.git.diff('HEAD', '--', file_path)
                if diff:
                    tracked_changes.append(TrackedChange(
                        path=file_path,
                        diff=diff
                    ))
        
        # Get untracked files with their content
        for file_path in repo.untracked_files:
            full_path = self.work_dir / file_path
            try:
                with open(full_path, 'r') as f:
                    content = f.read()
                untracked_changes.append(UntrackedChange(
                    path=str(file_path),
                    content=content
                ))
            except Exception as e:
                self.logger.warning(f"Could not read untracked file {file_path}: {e}")
            
        return RepositoryChanges(
            tracked_changes=tracked_changes,
            untracked_changes=untracked_changes
        )

    async def execute_command(
        self, command: Command, args: Dict[str, Any], config: EnvironmentConfig
    ) -> CommandResult:
        """Execute a command in a configured environment"""
        try:
            if not self.container:
                self.setup_container(command, config.environment_vars)

            # Get the command to execute
            shell_command = command.get_execution_command(args)

            # Execute in container
            exit_code, output = self.run_in_container(shell_command)

            # Get repository changes after command execution
            changes = self.get_repository_changes()

            return CommandResult(
                success=exit_code == 0,
                message=f"Command {command.metadata.name} {'completed successfully' if exit_code == 0 else 'failed'}",
                exit_code=exit_code,
                output=output if exit_code == 0 else None,
                error=output if exit_code != 0 else None,
                changes=changes,
                data={"command_name": command.metadata.name}
            )
        except Exception as e:
            # Create an empty RepositoryChanges object for failed commands
            empty_changes = RepositoryChanges(tracked_changes=[], untracked_changes=[])
            return CommandResult(
                success=False,
                message=f"Failed to execute command: {str(e)}",
                data={"error": str(e)},
                changes=empty_changes
            )

    def run_in_container(self, command: str) -> Tuple[int, str]:
        """Execute a raw command in the Docker container"""
        if not self.container:
            raise ValueError("Container not set up. Call setup_container first.")

        # Wait for container to be ready
        self.container.reload()  # Refresh container state
        self.logger.info(f"Container status: {self.container.status}")
        if self.container.status != "running":
            self.container.start()

        # Use sh -c to ensure environment variables are expanded
        exit_code, output = self.container.exec_run(["/bin/sh", "-c", command])
        return exit_code, output.decode("utf-8")

    def cleanup(self) -> None:
        """Clean up resources - stop container and remove working directory"""
        if self.container:
            self.container.stop()
            self.container.remove()
            self.container = None

        if self.work_dir:
            import shutil

            shutil.rmtree(self.work_dir)
            self.work_dir = None

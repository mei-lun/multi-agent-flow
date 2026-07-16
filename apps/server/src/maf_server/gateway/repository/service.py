"""Repository facade used by runs and final review workflows."""


class RepositoryGateway:
    def prepare_workspace(self, repository_id: str, run_id: str) -> str:
        raise NotImplementedError

    def create_pull_request(self, run_id: str) -> str:
        raise NotImplementedError


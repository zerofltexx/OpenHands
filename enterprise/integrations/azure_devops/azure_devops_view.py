"""Azure DevOps webhook view layer.

Top half: payload parsing (`AzureDevOpsView`) and trigger detection
(`AzureDevOpsFactory`).

Bottom half: ResolverViewInterface dataclasses that drive conversation
initialization for work items and PR comments — mirroring github_view.py's
"factory + view classes in one file" structure.
"""

import re
from abc import abstractmethod
from typing import Any, Optional, Union
from uuid import UUID, uuid4

from integrations.azure_devops.azure_devops_types import (
    AzureDevOpsEventType,
    PullRequestCommentedPayload,
)
from integrations.models import Message
from integrations.resolver_context import ResolverUserContext
from integrations.types import ResolverViewInterface, UserData
from jinja2 import Environment
from pydantic.dataclasses import dataclass
from storage.saas_secrets_store import SaasSecretsStore

from openhands.agent_server.models import SendMessageRequest
from openhands.app_server.app_conversation.app_conversation_models import (
    AppConversationStartRequest,
    AppConversationStartTaskStatus,
    ConversationTrigger,
)
from openhands.app_server.config import get_app_conversation_service
from openhands.app_server.integrations.provider import PROVIDER_TOKEN_TYPE, ProviderType
from openhands.app_server.integrations.service_types import Comment
from openhands.app_server.services.injector import InjectorState
from openhands.app_server.user.specifiy_user_context import USER_CONTEXT_ATTR
from openhands.app_server.user_auth.user_auth import UserAuth
from openhands.app_server.utils.logger import openhands_logger as logger
from openhands.sdk import TextContent

# Azure DevOps mention pattern
# When mentioning service principal in Azure DevOps UI, it appears as @<GUID> in webhook payload
# Pattern: @<XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX>
# Any GUID mention sent to our webhook endpoint is assumed to be the OpenHands service principal
AZURE_MENTION_PATTERN = re.compile(
    r'@<[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}>'
)


class AzureDevOpsView:
    """View model for Azure DevOps webhook payloads."""

    @staticmethod
    def parse_pr_commented(payload: dict) -> Optional[PullRequestCommentedPayload]:
        """Parse git.pullrequest.commented webhook payload.

        Args:
            payload: Raw webhook payload dictionary

        Returns:
            Typed payload or None if invalid
        """
        try:
            event_type = payload.get('eventType')
            if event_type != AzureDevOpsEventType.PR_COMMENTED:
                logger.warning(
                    f'Expected git.pullrequest.commented event, got {event_type}'
                )
                return None

            # Validate required fields
            resource = payload.get('resource', {})
            if not resource.get('pullRequestId'):
                logger.warning('Missing pull request ID in payload')
                return None

            return payload  # type: ignore

        except Exception as e:
            logger.exception(f'Error parsing PR commented payload: {str(e)}')
            return None

    @staticmethod
    def extract_pr_info(payload: PullRequestCommentedPayload) -> dict[str, Any]:
        """Extract key information from PR commented payload.

        Args:
            payload: Parsed PR commented payload

        Returns:
            Dictionary with extracted information
        """
        resource = payload['resource']
        containers = payload['resourceContainers']
        repository = resource.get('repository', {})

        return {
            'pull_request_id': resource['pullRequestId'],
            'repository_id': repository.get('id', ''),
            'repository_name': repository.get('name', ''),
            'project': repository.get('project', {}).get('name', ''),
            'title': resource.get('title', ''),
            'description': resource.get('description', ''),
            'account_id': containers.get('account', {}).get('id', ''),
            'url': resource.get('url', ''),
        }

    @staticmethod
    def get_event_type(payload: dict) -> Optional[AzureDevOpsEventType]:
        """Get the event type from a webhook payload.

        Args:
            payload: Raw webhook payload dictionary

        Returns:
            Event type enum or None if invalid
        """
        event_type_str = payload.get('eventType')
        try:
            return AzureDevOpsEventType(event_type_str)
        except ValueError:
            logger.warning(f'Unknown Azure DevOps event type: {event_type_str}')
            return None


class AzureDevOpsFactory:
    """Factory methods for detecting different Azure DevOps webhook event types."""

    @staticmethod
    def is_assigned_work_item(
        message: Message, service_principal_id: str | None = None
    ) -> bool:
        """Check if a work item is assigned to the OpenHands service principal.

        When a user assigns a work item to OpenHands in the Azure DevOps UI
        (via the "Assigned To" dropdown), the webhook payload includes the
        assigned identity in System.AssignedTo field.

        Handles both:
        - workitem.created: Assignment set during work item creation
        - workitem.updated: Assignment changed on existing work item

        Args:
            message: The webhook message
            service_principal_id: Optional service principal GUID to check for assignment

        Returns:
            True if work item is assigned to service principal, False otherwise
        """
        payload = message.message if isinstance(message.message, dict) else {}
        event_type = payload.get('eventType')

        # Handle both created and updated events
        if event_type not in (
            AzureDevOpsEventType.WORKITEM_UPDATED,
            AzureDevOpsEventType.WORKITEM_CREATED,
        ):
            return False

        # Check if work item is assigned to service principal
        resource = payload.get('resource', {})

        # For workitem.created, the assignment is in resource.fields directly (not a change object)
        # For workitem.updated, it's in resource.fields as a change object with oldValue/newValue
        if event_type == AzureDevOpsEventType.WORKITEM_CREATED:
            # workitem.created: fields contain direct values
            fields = resource.get('fields', {})
            assigned_to_value = fields.get('System.AssignedTo', '')

            logger.debug(
                f'[is_assigned_work_item] CREATED event, System.AssignedTo: {assigned_to_value}'
            )

            if not assigned_to_value:
                return False

            # Direct string value: "DisplayName <GUID>"
            assigned_value = assigned_to_value
        else:
            # workitem.updated: fields contain change objects
            fields = resource.get('fields', {})

            logger.debug(
                f'[is_assigned_work_item] UPDATED event, fields keys: {list(fields.keys())}'
            )

            # System.AssignedTo in fields is a change object with oldValue and newValue
            # Format: {'oldValue': 'User <guid>', 'newValue': 'OpenHands <guid>'}
            assigned_to_change = fields.get('System.AssignedTo')

            if not assigned_to_change:
                logger.debug(
                    '[is_assigned_work_item] No System.AssignedTo field in webhook, returning False'
                )
                return False

            logger.debug(
                f'[is_assigned_work_item] System.AssignedTo field: {assigned_to_change}'
            )

            # Check if this is an assignment change (has newValue)
            new_value = assigned_to_change.get('newValue')
            if not new_value:
                logger.debug(
                    '[is_assigned_work_item] No newValue in System.AssignedTo, returning False'
                )
                return False

            assigned_value = new_value

        logger.debug(
            f'[is_assigned_work_item] Trying to extract GUID from: {assigned_value}'
        )

        # Extract GUID from string format: "DisplayName <GUID>"
        # Use regex to extract GUID from angle brackets
        import re

        guid_match = re.search(r'<([a-fA-F0-9\-]+)>', assigned_value)

        if not guid_match:
            logger.warning(
                f'[is_assigned_work_item] Could not extract GUID from: {assigned_value}'
            )
            return False

        assigned_id = guid_match.group(1).upper()

        # If service principal ID is provided, check if assignment matches
        if service_principal_id:
            # Handle comma-separated IDs (memberId,subjectId)
            sp_ids = [id.strip().upper() for id in service_principal_id.split(',')]
            if assigned_id in sp_ids:
                logger.info(
                    f'[is_assigned_work_item] Work item assigned to service principal (ID: {assigned_id})'
                )
                return True
            return False

        # Fallback: if no service principal ID provided, accept any assignment
        # (This case should not happen in production)
        logger.warning(
            '[Azure DevOps] No service principal ID provided, cannot verify assignment'
        )
        return False

    @staticmethod
    def is_pr_comment(
        message: Message, inline: bool = False, service_principal_id: str | None = None
    ) -> bool:
        """Check if a PR was commented on with service principal mention.

        Args:
            message: The webhook message
            inline: If True, check for inline (code review) comments.
                   If False, check for general PR comments.
            service_principal_id: Optional service principal GUID to check for mentions

        Returns:
            True if PR comment contains service principal mention, False otherwise
        """
        payload = message.message if isinstance(message.message, dict) else {}
        event_type = payload.get('eventType')

        if event_type != AzureDevOpsEventType.PR_COMMENTED:
            return False

        # Get comment from payload
        resource = payload.get('resource', {})
        comment_data = resource.get('comment', {})

        # Check if this is an inline comment based on threadContext
        thread_context = comment_data.get('threadContext')
        is_inline_comment = thread_context is not None

        # Filter based on inline parameter
        if inline and not is_inline_comment:
            return False
        if not inline and is_inline_comment:
            return False

        # Check for service principal mention
        # Azure DevOps uses "content" for the comment text
        comment_text = comment_data.get('content') or comment_data.get('text', '')

        # If service principal ID is provided, do direct string matching
        if service_principal_id:
            # Handle comma-separated IDs (memberId,subjectId)
            sp_ids = [id.strip().upper() for id in service_principal_id.split(',')]

            # Check if any of the IDs are mentioned as @<ID>
            for sp_id in sp_ids:
                mention_pattern = f'@<{sp_id}>'
                if mention_pattern.upper() in comment_text.upper():
                    return True

            return False

        # Fallback: if no service principal ID provided, check for any GUID mention using regex
        guid_matches = AZURE_MENTION_PATTERN.findall(comment_text)
        has_mention = bool(guid_matches)
        return has_mention

    @staticmethod
    def is_inline_pr_comment(
        message: Message, service_principal_id: str | None = None
    ) -> bool:
        """Check if an inline PR comment (code review comment) contains service principal mention.

        Inline comments are comments on specific lines of code in a PR.
        They include threadContext with file path and line position.

        Args:
            message: The webhook message
            service_principal_id: Optional service principal GUID to check for mentions

        Returns:
            True if inline PR comment contains service principal mention, False otherwise
        """
        return AzureDevOpsFactory.is_pr_comment(
            message, inline=True, service_principal_id=service_principal_id
        )

    @staticmethod
    def is_work_item_comment(
        message: Message, service_principal_id: str | None = None
    ) -> bool:
        """Check if a work item comment contains service principal mention.

        When a user adds a comment to a work item in Azure DevOps UI,
        the webhook payload includes the comment text in System.History field.

        Work item mentions use HTML format:
        <a href="#" data-vss-mention="version:2.0,{guid}">@DisplayName</a>

        The GUID in mentions is the memberId, not the subjectId.

        Args:
            message: The webhook message
            service_principal_id: Service principal IDs (can be comma-separated: "memberId,subjectId")

        Returns:
            True if work item comment contains service principal mention, False otherwise
        """
        payload = message.message if isinstance(message.message, dict) else {}
        event_type = payload.get('eventType')

        if event_type != AzureDevOpsEventType.WORKITEM_UPDATED:
            return False

        # Service principal ID is required
        if not service_principal_id:
            logger.warning(
                '[is_work_item_comment] No service principal ID provided, cannot verify mention'
            )
            return False

        # Check if this update includes a comment (System.History field)
        resource = payload.get('resource', {})
        fields = resource.get('fields', {})

        # System.History contains the comment text when a comment is added
        history_change = fields.get('System.History')

        if not history_change:
            logger.debug(
                '[is_work_item_comment] No System.History field, returning False'
            )
            return False

        # System.History is a change object with newValue containing the comment
        comment_text = history_change.get('newValue', '')

        if not comment_text:
            logger.debug(
                '[is_work_item_comment] No newValue in System.History, returning False'
            )
            return False

        logger.debug(f'[is_work_item_comment] Comment text: {comment_text[:200]}...')

        # Parse service principal IDs (can be comma-separated: "memberId,subjectId")
        sp_ids = [id.strip().upper() for id in service_principal_id.split(',')]
        logger.debug(
            f'[is_work_item_comment] Checking against service principal IDs: {sp_ids}'
        )

        # Webhook System.History.newValue contains mentions in @<GUID> format
        # e.g., "@<FCE00FBE-ECC4-628A-A160-15AB328AAA40> remove tests"
        comment_text_upper = comment_text.upper()
        for sp_id in sp_ids:
            mention_pattern = f'@<{sp_id}>'
            if mention_pattern in comment_text_upper:
                logger.info(
                    f'[is_work_item_comment] Found service principal mention (matched {sp_id})'
                )
                return True

        logger.debug(
            f'[is_work_item_comment] Service principal IDs {sp_ids} not found in comment'
        )
        return False


@dataclass
class AzureDevOpsBase(ResolverViewInterface):
    """Base class for Azure DevOps views with common fields and methods.

    This class contains all shared functionality between work items and PR comments,
    eliminating code duplication across view types.
    """

    # Common identifiers
    organization: str
    project_name: str
    full_repo_name: str  # Format: org/project/repo

    # Repository info
    is_public_repo: bool

    # User and payload
    user_info: UserData
    raw_payload: Message

    # Conversation tracking
    conversation_id: str
    uuid: str | None

    # Behavior flags
    should_extract: bool
    send_summary_instruction: bool

    # Content
    title: str
    description: str
    previous_comments: list[Comment]

    # Required by ResolverViewInterface (Azure DevOps doesn't have installation_id)
    # Callers must pass 0 explicitly since Azure DevOps doesn't use installation_id
    installation_id: int
    # issue_number is set to work_item_id or pr_id by callers
    issue_number: int

    async def _get_user_secrets(self):
        """Get user secrets from the SaaS secrets store."""
        secrets_store = await SaasSecretsStore.get_instance(
            self.user_info.keycloak_user_id
        )
        user_secrets = await secrets_store.load()
        return user_secrets.custom_secrets if user_secrets else None

    @abstractmethod
    async def _get_instructions(self, jinja_env: Environment) -> tuple[str, str]:
        """Get user and conversation instructions. Must be implemented by subclasses."""
        pass

    @abstractmethod
    def _get_conversation_title(self) -> str:
        """Get the conversation title. Must be implemented by subclasses."""
        pass

    @abstractmethod
    def _get_selected_branch(self) -> str | None:
        """Get the selected branch name. Must be implemented by subclasses."""
        pass

    @abstractmethod
    def _create_azure_devops_v1_callback_processor(self):
        """Create a V1 callback processor. Must be implemented by subclasses."""
        pass

    async def initialize_new_conversation(self) -> UUID:
        """Allocate a fresh conversation ID for this view."""
        conversation_id = uuid4()
        self.conversation_id = conversation_id.hex
        return conversation_id

    async def create_new_conversation(
        self,
        jinja_env: Environment,
        git_provider_tokens: PROVIDER_TOKEN_TYPE,
        conversation_id: UUID,
        saas_user_auth: UserAuth,
    ):
        """Create and start a new V1 conversation for this view."""
        logger.info('[Azure DevOps V1]: Creating V1 conversation')

        user_instructions, conversation_instructions = await self._get_instructions(
            jinja_env
        )

        initial_message = SendMessageRequest(
            role='user', content=[TextContent(text=user_instructions)]
        )

        azure_devops_callback_processor = (
            self._create_azure_devops_v1_callback_processor()
        )

        injector_state = InjectorState()

        start_request = AppConversationStartRequest(
            conversation_id=conversation_id,
            system_message_suffix=conversation_instructions,
            initial_message=initial_message,
            selected_repository=self.full_repo_name,
            selected_branch=self._get_selected_branch(),
            git_provider=ProviderType.AZURE_DEVOPS,
            title=self._get_conversation_title(),
            trigger=ConversationTrigger.RESOLVER,
            processors=[azure_devops_callback_processor],
        )

        azure_devops_user_context = ResolverUserContext(saas_user_auth=saas_user_auth)
        setattr(injector_state, USER_CONTEXT_ATTR, azure_devops_user_context)

        async with get_app_conversation_service(
            injector_state
        ) as app_conversation_service:
            async for task in app_conversation_service.start_app_conversation(
                start_request
            ):
                if task.status == AppConversationStartTaskStatus.ERROR:
                    logger.error(f'Failed to start V1 conversation: {task.detail}')
                    raise RuntimeError(
                        f'Failed to start V1 conversation: {task.detail}'
                    )


# Common default/protected branch names
DEFAULT_BRANCH_NAMES = frozenset(
    {
        'main',
        'master',
        'develop',
        'development',
        'dev',
        'trunk',
        'release',
        'production',
        'prod',
        'staging',
    }
)


def is_default_branch(branch_name: str | None) -> bool:
    """Check if a branch name is a default/protected branch."""
    if not branch_name:
        return False
    normalized = branch_name.lower().strip()
    if normalized.startswith('refs/heads/'):
        normalized = normalized[len('refs/heads/') :]
    return normalized in DEFAULT_BRANCH_NAMES


@dataclass
class AzureDevOpsWorkItem(AzureDevOpsBase):
    """View for an Azure DevOps Work Item (Bug, Task, User Story, etc.) with @openhands mention or assignment."""

    work_item_id: int
    work_item_type: str  # Bug, Task, User Story, etc.
    selected_branch: str | None = None  # Branch from work item development section
    comment_body: str | None = (
        None  # Comment text when triggered by @mention in comment
    )
    repository_linked: bool = (
        True  # Whether work item has a linked repository in development section
    )

    def _get_conversation_title(self) -> str:
        return f'Azure DevOps Work Item #{self.work_item_id}: {self.title}'

    def _get_selected_branch(self) -> str | None:
        return self.selected_branch

    async def _get_instructions(self, jinja_env: Environment) -> tuple[str, str]:
        user_instructions_template = jinja_env.get_template('issue_prompt.j2')
        user_instructions = user_instructions_template.render(
            issue_comment=self.comment_body, issue_number=self.work_item_id
        )

        context = (
            f'Please address this Azure DevOps {self.work_item_type}:\n'
            f'Title: {self.title}\n'
            f'Description: {self.description}\n\n'
            f'Repository: {self.full_repo_name}\n'
            f'Work Item URL: https://dev.azure.com/{self.organization}/{self.project_name}/_workitems/edit/{self.work_item_id}\n'
        )
        user_instructions = context + '\n' + user_instructions

        conversation_instructions_template = jinja_env.get_template(
            'issue_conversation_instructions.j2'
        )
        conversation_instructions = conversation_instructions_template.render(
            issue_number=self.work_item_id,
            issue_title=self.title,
            issue_body=self.description,
            previous_comments=self.previous_comments,
            selected_branch=self.selected_branch,
            repository_linked=self.repository_linked,
            is_default_branch=is_default_branch(self.selected_branch),
        )
        return user_instructions, conversation_instructions

    def _create_azure_devops_v1_callback_processor(self):
        from integrations.azure_devops.azure_devops_v1_callback_processor import (
            AzureDevOpsV1CallbackProcessor,
        )

        return AzureDevOpsV1CallbackProcessor(
            azure_devops_view_data={
                'work_item_id': self.work_item_id,
                'full_repo_name': self.full_repo_name,
                'organization': self.organization,
                'project_name': self.project_name,
            },
            should_request_summary=self.send_summary_instruction,
            is_pr_comment=False,
            thread_id=None,
        )


@dataclass
class AzureDevOpsPRComment(AzureDevOpsBase):
    """View for an Azure DevOps Pull Request general discussion comment with @openhands mention."""

    pr_id: int
    repository_name: str
    branch_name: str | None  # PR source branch name
    thread_id: int | None = None  # Thread ID for replying to the original thread

    def _get_conversation_title(self) -> str:
        return f'Azure DevOps PR #{self.pr_id}: {self.title}'

    def _get_selected_branch(self) -> str | None:
        return self.branch_name

    def _get_pr_url(self) -> str:
        return (
            f'https://dev.azure.com/{self.organization}/{self.project_name}'
            f'/_git/{self.repository_name}/pullrequest/{self.pr_id}'
        )

    async def _get_instructions(self, jinja_env: Environment) -> tuple[str, str]:
        user_instructions = (
            f'Please address this Azure DevOps Pull Request discussion comment:\n'
            f'PR #{self.pr_id}: {self.title}\n'
            f'Description: {self.description}\n\n'
            f'Repository: {self.full_repo_name}\n'
            f'PR URL: {self._get_pr_url()}\n'
        )

        if self.previous_comments:
            user_instructions += '\n\nPrevious comments:\n'
            for comment in self.previous_comments:
                user_instructions += f'- {comment.author}: {comment.body}\n'

        conversation_instructions_template = jinja_env.get_template(
            'pr_update_conversation_instructions.j2'
        )
        conversation_instructions = conversation_instructions_template.render(
            pr_number=self.pr_id,
            pr_title=self.title,
            pr_body=self.description,
            previous_comments=self.previous_comments,
            branch_name=self.branch_name,
        )
        return user_instructions, conversation_instructions

    def _create_azure_devops_v1_callback_processor(self):
        from integrations.azure_devops.azure_devops_v1_callback_processor import (
            AzureDevOpsV1CallbackProcessor,
        )

        return AzureDevOpsV1CallbackProcessor(
            azure_devops_view_data={
                'pr_id': self.pr_id,
                'full_repo_name': self.full_repo_name,
                'organization': self.organization,
                'project_name': self.project_name,
                'repository_name': self.repository_name,
            },
            should_request_summary=self.send_summary_instruction,
            is_pr_comment=True,
            thread_id=self.thread_id,
        )


@dataclass
class AzureDevOpsInlinePRComment(AzureDevOpsPRComment):
    """View for an Azure DevOps Pull Request inline code review comment with file/line context."""

    thread_context: dict | None = None

    async def _get_instructions(self, jinja_env: Environment) -> tuple[str, str]:
        user_instructions = (
            f'Please address this Azure DevOps Pull Request inline code review comment:\n'
            f'PR #{self.pr_id}: {self.title}\n'
            f'Description: {self.description}\n\n'
            f'Repository: {self.full_repo_name}\n'
            f'PR URL: {self._get_pr_url()}\n'
        )

        if self.thread_context:
            file_path = self.thread_context.get('filePath', 'unknown')
            right_line = self.thread_context.get('rightFileEnd', {}).get(
                'line', 'unknown'
            )
            user_instructions += (
                f'\nInline comment location: {file_path}:{right_line}\n'
            )

        if self.previous_comments:
            user_instructions += '\n\nPrevious comments:\n'
            for comment in self.previous_comments:
                user_instructions += f'- {comment.author}: {comment.body}\n'

        conversation_instructions_template = jinja_env.get_template(
            'pr_update_conversation_instructions.j2'
        )
        conversation_instructions = conversation_instructions_template.render(
            pr_number=self.pr_id,
            pr_title=self.title,
            pr_body=self.description,
            previous_comments=self.previous_comments,
            branch_name=self.branch_name,
            file_location=self.thread_context.get('filePath')
            if self.thread_context
            else None,
            line_number=self.thread_context.get('rightFileEnd', {}).get('line')
            if self.thread_context
            else None,
        )
        return user_instructions, conversation_instructions


# Type alias for all Azure DevOps view types
AzureDevOpsViewType = Union[
    AzureDevOpsWorkItem, AzureDevOpsPRComment, AzureDevOpsInlinePRComment
]

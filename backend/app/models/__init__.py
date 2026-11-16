"""Register the standalone transcription database schema."""

from app.models.audio_object import AudioObject  # noqa: F401
from app.models.background_job import BackgroundJob  # noqa: F401
from app.models.compliance_export import ComplianceExportJob  # noqa: F401
from app.models.meeting_clip import MeetingClip  # noqa: F401
from app.models.meeting_comment import MeetingComment  # noqa: F401
from app.models.meeting_dlp import (
    DlpFinding,  # noqa: F401
    DlpTerm,  # noqa: F401
)
from app.models.meeting_governance import (
    LegalHold,  # noqa: F401
    MeetingAuditEvent,  # noqa: F401
    MeetingPermissionGrant,  # noqa: F401
    RetentionPolicy,  # noqa: F401
)
from app.models.meeting_note import (
    MeetingNote,  # noqa: F401
    NoteAgent,  # noqa: F401
)
from app.models.meeting_recording import MeetingRecording  # noqa: F401
from app.models.meeting_transcript_revision import (
    MeetingTranscriptEditEvent,  # noqa: F401
    MeetingTranscriptRevision,  # noqa: F401
)
from app.models.meeting_transcript_segment import MeetingTranscriptSegment  # noqa: F401
from app.models.meeting_transcription_attempt import (
    MeetingTranscriptionAttempt,  # noqa: F401
)
from app.models.meeting_upload_session import (
    MeetingUploadPart,  # noqa: F401
    MeetingUploadSession,  # noqa: F401
)
from app.models.notification import Notification  # noqa: F401
from app.models.speaker_consent import (
    SpeakerConsentRecord,  # noqa: F401
    SpeakerSuggestion,  # noqa: F401
)
from app.models.speaker_name_run import SpeakerNameRun  # noqa: F401
from app.models.speaker_profile import (
    SpeakerProfile,  # noqa: F401
    SpeakerVoiceprint,  # noqa: F401
)
from app.models.study import (
    StudyNote,  # noqa: F401
    StudyNotebook,  # noqa: F401
    StudySession,  # noqa: F401
)
from app.models.task_outbox import TaskOutbox  # noqa: F401
from app.models.transcript_cleanup import TranscriptCleanup  # noqa: F401
from app.models.user import (
    Role,  # noqa: F401
    User,  # noqa: F401
)
from app.models.workspace import (
    Workspace,  # noqa: F401
    WorkspaceMember,  # noqa: F401
)
from app.models.workspace_activity_event import WorkspaceActivityEvent  # noqa: F401

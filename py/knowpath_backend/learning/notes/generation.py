"""Resolve configured models at job execution, keeping provider failures isolated."""
from knowpath_backend.learning.config import LearningSettings
from knowpath_backend.learning.providers.models import chat_model


class ConfiguredNoteGenerator:
    def __init__(self, settings=None):
        self.settings = settings or LearningSettings.from_env()

    def generate_json(self, messages):
        return chat_model(self.settings).generate_json(messages)

    def stream(self,messages):
        yield from chat_model(self.settings).stream(messages,response_format={'type':'json_object'})

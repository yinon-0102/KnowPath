from my_agent_llms.learning.config import LearningSettings


def test_learning_settings_use_dashscope_embedding_defaults(monkeypatch):
    monkeypatch.delenv("LEARNING_EMBEDDING_PROVIDER", raising=False)
    monkeypatch.delenv("LEARNING_EMBEDDING_MODEL", raising=False)

    settings = LearningSettings.from_env()

    assert settings.embedding_provider == "dashscope"
    assert settings.embedding_model == "text-embedding-v3"

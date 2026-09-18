from my_agent_llms.learning.storage import GraphSettings, VectorSettings


def test_graph_and_vector_settings_have_local_defaults():
    graph = GraphSettings.from_env()
    vector = VectorSettings.from_env()

    assert graph.uri == "bolt://127.0.0.1:7687"
    assert vector.url == "http://127.0.0.1:6333"
    assert vector.collection == "keel_material_chunks"

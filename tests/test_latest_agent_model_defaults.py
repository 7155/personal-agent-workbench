from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rag_ime.agent_configuration import AgentConfigurationStore, default_agent_configuration
from rag_ime.agent_model_defaults import DEFAULT_AGENT_MODEL_PROFILE
from rag_ime.agent_roles import agent_role_catalog
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.memory_maintenance_settings import MemoryMaintenanceSettings
from rag_ime.pi.model_additions import with_current_codex_models


class LatestAgentModelDefaultsTests(unittest.TestCase):
    def test_new_work_uses_the_available_current_model_across_all_default_owners(self):
        configuration = default_agent_configuration()
        self.assertEqual(configuration['sessionDefaults']['modelProfile'], 'openai-codex/gpt-6.1-sol')
        self.assertTrue(all(route['modelProfile'] == DEFAULT_AGENT_MODEL_PROFILE
                            for route in configuration['modelRouting'].values()))
        self.assertTrue(all(role['defaults']['modelProfile'] == DEFAULT_AGENT_MODEL_PROFILE
                            for role in agent_role_catalog()))
        memory = MemoryMaintenanceSettings()
        self.assertEqual(memory.automatic_organization_model, DEFAULT_AGENT_MODEL_PROFILE)
        self.assertEqual(memory.dreaming_model, DEFAULT_AGENT_MODEL_PROFILE)
        models = with_current_codex_models({})['openai-codex']['models']
        self.assertIn('gpt-6.1-sol', [model['id'] for model in models])

    def test_startup_upgrades_default_routes_once_without_rewriting_existing_sessions(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / 'state.sqlite'
            old = default_agent_configuration(model_profile='openai-codex/gpt-5.6-luna')
            old['modelRouting']['primary'] = {'modelProfile': 'openai-codex/gpt-5.6-sol', 'thinkingLevel': 'high'}
            old['modelRouting']['toolAgent'] = {'modelProfile': 'custom/model-explicit', 'thinkingLevel': 'low'}
            # Seed the released shape without running its new startup migrator.
            with patch('rag_ime.agent_configuration.PREVIOUS_PRODUCT_MODEL_PROFILES', frozenset()):
                AgentConfigurationStore(db).initialize(old)
            sessions = AgentSessionStore(db)
            session = sessions.create(title='Recorded old Session', role_id='companion-present-v1', role_version='1',
                                              model_profile='openai-codex/gpt-5.6-terra')
            store = AgentConfigurationStore(db)
            store.initialize(default_agent_configuration())
            snapshot = store.snapshot()
            self.assertEqual(snapshot['configuration']['sessionDefaults']['modelProfile'], DEFAULT_AGENT_MODEL_PROFILE)
            self.assertEqual(snapshot['configuration']['modelRouting']['primary'],
                             {'modelProfile': DEFAULT_AGENT_MODEL_PROFILE, 'thinkingLevel': 'high'})
            self.assertEqual(snapshot['configuration']['modelRouting']['toolAgent']['modelProfile'], 'custom/model-explicit')
            self.assertEqual(sessions.get(session['id'])['modelProfile'], 'openai-codex/gpt-5.6-terra')
            revision = snapshot['revision']
            store.initialize(default_agent_configuration())
            self.assertEqual(store.snapshot()['revision'], revision)

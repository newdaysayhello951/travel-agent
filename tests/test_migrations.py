import unittest
from sqlalchemy import create_engine,text
from db_migrations import upgrade_database
from alembic.config import Config
from alembic import command
from pathlib import Path


class MigrationTests(unittest.TestCase):
    def test_legacy_rows_survive_adoption_and_repeat_upgrade(self):
        engine=create_engine('sqlite://')
        cfg=Config();cfg.set_main_option('script_location',str(Path(__file__).resolve().parents[1]/'migrations'))
        with engine.begin() as connection:
            cfg.attributes['connection']=connection;command.upgrade(cfg,'0001')
            connection.execute(text('DROP TABLE alembic_version'))
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO travel_conversations VALUES ('legacy','old',0,CURRENT_TIMESTAMP)"))
        upgrade_database(engine);upgrade_database(engine)
        with engine.connect() as connection:
            self.assertEqual(connection.execute(text('SELECT title FROM travel_conversations')).scalar(),'old')
            self.assertIsNotNone(connection.execute(text('SELECT version_num FROM alembic_version')).scalar())
        engine.dispose()

    def test_partial_legacy_schema_is_not_silently_stamped(self):
        engine=create_engine('sqlite://')
        with engine.begin() as c:c.execute(text('CREATE TABLE travel_conversations (id TEXT)'))
        with self.assertRaises(RuntimeError):upgrade_database(engine)
        engine.dispose()

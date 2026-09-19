-- KnowPath MySQL 空库初始化脚本
-- 生成日期：2026-09-19；对应 Alembic 版本：0013_material_raw。
-- 适用 MySQL 8.4，包含 25 张应用表及 alembic_version 表。
-- 来源：已完成全部 migrations 的数据库结构，并与 SQLAlchemy 模型核对。
-- 仅用于全新空库；已有数据库请在 py/ 下执行 uv run alembic upgrade head。
-- 请勿使用客户端的“忽略错误/继续执行”选项；建表报错时应立即停止。
-- MySQL DDL 不保证整份脚本原子执行；若中断，请检查或重新准备空库。
-- 如需使用其他库名，请修改下面 CREATE DATABASE 和 USE 两处。
-- 在 MySQL 客户端中运行：SOURCE C:/Users/27202/Desktop/KnowPath/py/init_mysql.sql;
-- 也可通过数据库管理工具连接 MySQL 后打开并执行本文件。
-- 不包含业务数据、用户密码、Neo4j 节点或 Qdrant 索引。
-- 无需手动 stamp：末尾写入迁移版本，后续继续使用 Alembic 升级。
-- 表结构演进以 migrations/ 为准，本文件是上述版本的初始化快照。

SET NAMES utf8mb4;
CREATE DATABASE IF NOT EXISTS `keel_learning` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE `keel_learning`;

CREATE TABLE `alembic_version` (
  `version_num` varchar(32) NOT NULL,
  PRIMARY KEY (`version_num`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `idempotency_keys` (
  `key` varchar(255) NOT NULL,
  `request_fingerprint` varchar(64) NOT NULL,
  `resource_type` varchar(64) NOT NULL,
  `resource_id` varchar(128) NOT NULL,
  `version_id` varchar(36) DEFAULT NULL,
  `run_id` varchar(36) DEFAULT NULL,
  `created_at` datetime NOT NULL,
  `response` json DEFAULT NULL,
  PRIMARY KEY (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `knowledge_relations` (
  `id` varchar(128) NOT NULL,
  `revision_id` varchar(128) NOT NULL,
  `from_topic_id` varchar(128) NOT NULL,
  `to_topic_id` varchar(128) NOT NULL,
  `relation_type` varchar(64) NOT NULL,
  `status` varchar(32) NOT NULL,
  `source_refs` json NOT NULL,
  `confidence` float NOT NULL,
  `valid_from` datetime DEFAULT NULL,
  `valid_to` datetime DEFAULT NULL,
  `recorded_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_knowledge_relations_from_topic_id` (`from_topic_id`),
  KEY `ix_knowledge_relations_to_topic_id` (`to_topic_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `learning_spaces` (
  `id` varchar(36) NOT NULL,
  `name` varchar(100) NOT NULL,
  `status` varchar(32) NOT NULL,
  `goal` text,
  `target_date` varchar(32) DEFAULT NULL,
  `weekly_minutes` int DEFAULT NULL,
  `bindings` json NOT NULL,
  `topic_ids` json NOT NULL,
  `excluded_topic_ids` json NOT NULL,
  `space_version` int NOT NULL,
  `scope_version` int NOT NULL,
  `profile_version` int NOT NULL,
  `state_version` int NOT NULL,
  `created_at` datetime NOT NULL,
  `updated_at` datetime NOT NULL,
  `profile` json NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `materials` (
  `id` varchar(36) NOT NULL,
  `name` varchar(255) NOT NULL,
  `type` varchar(32) NOT NULL,
  `status` varchar(32) NOT NULL,
  `current_version_id` varchar(36) DEFAULT NULL,
  `size_bytes` int NOT NULL,
  `created_at` datetime NOT NULL,
  `updated_at` datetime NOT NULL,
  `version` int NOT NULL DEFAULT '1',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `outbox_events` (
  `id` varchar(36) NOT NULL,
  `aggregate_type` varchar(64) NOT NULL,
  `aggregate_id` varchar(128) NOT NULL,
  `event_type` varchar(64) NOT NULL,
  `payload` json NOT NULL,
  `status` varchar(32) NOT NULL,
  `created_at` datetime NOT NULL,
  `lease_token` varchar(36) DEFAULT NULL,
  `lease_until` datetime DEFAULT NULL,
  `available_at` datetime DEFAULT NULL,
  `attempts` int NOT NULL DEFAULT '0',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `runs` (
  `id` varchar(36) NOT NULL,
  `kind` varchar(64) NOT NULL,
  `status` varchar(32) NOT NULL,
  `progress` int NOT NULL,
  `result_ref` json DEFAULT NULL,
  `error` json DEFAULT NULL,
  `created_at` datetime NOT NULL,
  `finished_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `assessments` (
  `id` varchar(36) NOT NULL,
  `space_id` varchar(36) NOT NULL,
  `kind` varchar(32) NOT NULL,
  `status` varchar(32) NOT NULL,
  `topic_ids` json NOT NULL,
  `questions` json NOT NULL,
  `result` json DEFAULT NULL,
  `created_at` datetime NOT NULL,
  `snapshot` json NOT NULL,
  `run_id` varchar(36) DEFAULT NULL,
  `finalize_run_id` varchar(36) DEFAULT NULL,
  `submission_id` varchar(36) DEFAULT NULL,
  `context` json NOT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_assessments_space_id` (`space_id`),
  CONSTRAINT `assessments_ibfk_1` FOREIGN KEY (`space_id`) REFERENCES `learning_spaces` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `knowledge_corrections` (
  `id` varchar(36) NOT NULL,
  `space_id` varchar(36) NOT NULL,
  `kind` varchar(32) NOT NULL,
  `target_id` varchar(128) NOT NULL,
  `action` varchar(32) NOT NULL,
  `proposed_value` json DEFAULT NULL,
  `reason` text NOT NULL,
  `status` varchar(32) NOT NULL,
  `created_at` datetime NOT NULL,
  `context` json NOT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_knowledge_corrections_space_id` (`space_id`),
  CONSTRAINT `knowledge_corrections_ibfk_1` FOREIGN KEY (`space_id`) REFERENCES `learning_spaces` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `learner_states` (
  `id` varchar(36) NOT NULL,
  `space_id` varchar(36) NOT NULL,
  `topic_id` varchar(128) NOT NULL,
  `mastery_score` float DEFAULT NULL,
  `score_validity` varchar(32) NOT NULL,
  `status` varchar(32) NOT NULL,
  `evidence_ids` json NOT NULL,
  `error_tags` json NOT NULL,
  `state_version` int NOT NULL,
  `policy_version` varchar(64) NOT NULL,
  `last_assessed_at` datetime DEFAULT NULL,
  `next_review_at` datetime DEFAULT NULL,
  `context` json NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_learner_space_topic` (`space_id`,`topic_id`),
  KEY `ix_learner_states_space_id` (`space_id`),
  KEY `ix_learner_states_topic_id` (`topic_id`),
  CONSTRAINT `learner_states_ibfk_1` FOREIGN KEY (`space_id`) REFERENCES `learning_spaces` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `learning_exports` (
  `id` varchar(36) NOT NULL,
  `space_id` varchar(36) NOT NULL,
  `run_id` varchar(36) NOT NULL,
  `status` varchar(32) NOT NULL,
  `payload` json NOT NULL,
  `created_at` datetime NOT NULL,
  `expires_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  KEY `run_id` (`run_id`),
  KEY `ix_learning_exports_space_id` (`space_id`),
  KEY `ix_learning_exports_expires_at` (`expires_at`),
  CONSTRAINT `learning_exports_ibfk_1` FOREIGN KEY (`space_id`) REFERENCES `learning_spaces` (`id`),
  CONSTRAINT `learning_exports_ibfk_2` FOREIGN KEY (`run_id`) REFERENCES `runs` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `material_versions` (
  `id` varchar(36) NOT NULL,
  `material_id` varchar(36) NOT NULL,
  `filename` varchar(255) NOT NULL,
  `media_type` varchar(128) NOT NULL,
  `content_hash` varchar(64) NOT NULL,
  `size_bytes` int NOT NULL,
  `status` varchar(32) NOT NULL,
  `graph_version` int NOT NULL,
  `created_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_material_versions_material_id` (`material_id`),
  KEY `ix_material_versions_content_hash` (`content_hash`),
  CONSTRAINT `material_versions_ibfk_1` FOREIGN KEY (`material_id`) REFERENCES `materials` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `run_events` (
  `run_id` varchar(36) NOT NULL,
  `sequence` int NOT NULL,
  `event` varchar(64) NOT NULL,
  `data` json DEFAULT NULL,
  `created_at` datetime NOT NULL,
  PRIMARY KEY (`run_id`,`sequence`),
  KEY `ix_run_events_created_at` (`created_at`),
  CONSTRAINT `run_events_ibfk_1` FOREIGN KEY (`run_id`) REFERENCES `runs` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `state_resets` (
  `id` varchar(36) NOT NULL,
  `space_id` varchar(36) NOT NULL,
  `topic_ids` json NOT NULL,
  `state_version` int NOT NULL,
  `reason` text NOT NULL,
  `created_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_state_resets_space_id` (`space_id`),
  CONSTRAINT `state_resets_ibfk_1` FOREIGN KEY (`space_id`) REFERENCES `learning_spaces` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `study_plans` (
  `id` varchar(36) NOT NULL,
  `space_id` varchar(36) NOT NULL,
  `version` int NOT NULL,
  `status` varchar(32) NOT NULL,
  `created_at` datetime NOT NULL,
  `scope_version` int NOT NULL,
  `run_id` varchar(36) DEFAULT NULL,
  `config` json NOT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_study_plans_space_id` (`space_id`),
  CONSTRAINT `study_plans_ibfk_1` FOREIGN KEY (`space_id`) REFERENCES `learning_spaces` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `attempts` (
  `id` varchar(36) NOT NULL,
  `assessment_id` varchar(36) NOT NULL,
  `question_id` varchar(128) NOT NULL,
  `answer` text NOT NULL,
  `answer_revision` int NOT NULL,
  `assisted` tinyint(1) NOT NULL,
  `created_at` datetime NOT NULL,
  `elapsed_seconds` int DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_attempt_revision` (`assessment_id`,`question_id`,`answer_revision`),
  KEY `ix_attempts_assessment_id` (`assessment_id`),
  CONSTRAINT `attempts_ibfk_1` FOREIGN KEY (`assessment_id`) REFERENCES `assessments` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `evidence` (
  `id` varchar(36) NOT NULL,
  `space_id` varchar(36) NOT NULL,
  `topic_id` varchar(128) NOT NULL,
  `assessment_id` varchar(36) DEFAULT NULL,
  `attempt_id` varchar(36) DEFAULT NULL,
  `kind` varchar(64) NOT NULL,
  `result` varchar(32) NOT NULL,
  `score` float DEFAULT NULL,
  `error_tags` json NOT NULL,
  `source_refs` json NOT NULL,
  `created_at` datetime NOT NULL,
  `context` json NOT NULL,
  PRIMARY KEY (`id`),
  KEY `assessment_id` (`assessment_id`),
  KEY `ix_evidence_space_id` (`space_id`),
  KEY `ix_evidence_topic_id` (`topic_id`),
  CONSTRAINT `evidence_ibfk_1` FOREIGN KEY (`space_id`) REFERENCES `learning_spaces` (`id`),
  CONSTRAINT `evidence_ibfk_2` FOREIGN KEY (`assessment_id`) REFERENCES `assessments` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `graph_revisions` (
  `id` varchar(36) NOT NULL,
  `material_id` varchar(36) NOT NULL,
  `material_version_id` varchar(36) NOT NULL,
  `sequence` int NOT NULL,
  `base_graph_version` int NOT NULL,
  `graph_version` int DEFAULT NULL,
  `status` varchar(32) NOT NULL,
  `run_id` varchar(36) NOT NULL,
  `snapshot` json NOT NULL,
  `snapshot_hash` varchar(64) NOT NULL,
  `diff` json NOT NULL,
  `created_at` datetime NOT NULL,
  `preparation` json DEFAULT NULL,
  `publication` json DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_graph_revision_sequence` (`material_id`,`sequence`),
  UNIQUE KEY `uq_graph_published_version` (`material_id`,`graph_version`),
  KEY `material_version_id` (`material_version_id`),
  KEY `ix_graph_revisions_material_id` (`material_id`),
  CONSTRAINT `graph_revisions_ibfk_1` FOREIGN KEY (`material_id`) REFERENCES `materials` (`id`),
  CONSTRAINT `graph_revisions_ibfk_2` FOREIGN KEY (`material_version_id`) REFERENCES `material_versions` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `material_raw_files` (
  `version_id` varchar(36) NOT NULL,
  `content` longblob NOT NULL,
  PRIMARY KEY (`version_id`),
  CONSTRAINT `material_raw_files_ibfk_1` FOREIGN KEY (`version_id`) REFERENCES `material_versions` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `source_chunks` (
  `id` varchar(36) NOT NULL,
  `material_version_id` varchar(36) NOT NULL,
  `text` text NOT NULL,
  `section_path` json NOT NULL,
  `page` int DEFAULT NULL,
  `line_start` int DEFAULT NULL,
  `line_end` int DEFAULT NULL,
  `content_hash` varchar(64) NOT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_source_chunks_material_version_id` (`material_version_id`),
  KEY `ix_source_chunks_content_hash` (`content_hash`),
  CONSTRAINT `source_chunks_ibfk_1` FOREIGN KEY (`material_version_id`) REFERENCES `material_versions` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `study_tasks` (
  `id` varchar(36) NOT NULL,
  `plan_id` varchar(36) NOT NULL,
  `topic_ids` json NOT NULL,
  `kind` varchar(64) NOT NULL,
  `status` varchar(32) NOT NULL,
  `estimated_minutes` int NOT NULL,
  `reason` text NOT NULL,
  `context` json NOT NULL,
  `note` text,
  `defer_until` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_study_tasks_plan_id` (`plan_id`),
  CONSTRAINT `study_tasks_ibfk_1` FOREIGN KEY (`plan_id`) REFERENCES `study_plans` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `topics` (
  `id` varchar(128) NOT NULL,
  `material_version_id` varchar(36) NOT NULL,
  `revision_id` varchar(128) NOT NULL,
  `name` varchar(255) NOT NULL,
  `description` text,
  `level` int NOT NULL,
  `status` varchar(32) NOT NULL,
  `confidence` float NOT NULL,
  `source_chunk_ids` json NOT NULL,
  `valid_from` datetime DEFAULT NULL,
  `valid_to` datetime DEFAULT NULL,
  `recorded_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_topics_material_version_id` (`material_version_id`),
  CONSTRAINT `topics_ibfk_1` FOREIGN KEY (`material_version_id`) REFERENCES `material_versions` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `learning_sessions` (
  `id` varchar(36) NOT NULL,
  `space_id` varchar(36) NOT NULL,
  `plan_id` varchar(36) NOT NULL,
  `task_id` varchar(36) NOT NULL,
  `status` varchar(32) NOT NULL,
  `started_at` datetime NOT NULL,
  `finished_at` datetime DEFAULT NULL,
  `context` json NOT NULL,
  `active_space_id` varchar(36) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_learning_sessions_active_space_id` (`active_space_id`),
  KEY `plan_id` (`plan_id`),
  KEY `task_id` (`task_id`),
  KEY `ix_learning_sessions_space_id` (`space_id`),
  CONSTRAINT `learning_sessions_ibfk_1` FOREIGN KEY (`space_id`) REFERENCES `learning_spaces` (`id`),
  CONSTRAINT `learning_sessions_ibfk_2` FOREIGN KEY (`plan_id`) REFERENCES `study_plans` (`id`),
  CONSTRAINT `learning_sessions_ibfk_3` FOREIGN KEY (`task_id`) REFERENCES `study_tasks` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `learning_conversations` (
  `id` varchar(36) NOT NULL,
  `space_id` varchar(36) NOT NULL,
  `learning_session_id` varchar(36) DEFAULT NULL,
  `created_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  KEY `learning_session_id` (`learning_session_id`),
  KEY `ix_learning_conversations_space_id` (`space_id`),
  CONSTRAINT `learning_conversations_ibfk_1` FOREIGN KEY (`space_id`) REFERENCES `learning_spaces` (`id`),
  CONSTRAINT `learning_conversations_ibfk_2` FOREIGN KEY (`learning_session_id`) REFERENCES `learning_sessions` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `session_events` (
  `id` varchar(36) NOT NULL,
  `session_id` varchar(36) NOT NULL,
  `client_event_id` varchar(128) DEFAULT NULL,
  `type` varchar(64) NOT NULL,
  `payload` json NOT NULL,
  `received_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `client_event_id` (`client_event_id`),
  KEY `ix_session_events_session_id` (`session_id`),
  CONSTRAINT `session_events_ibfk_1` FOREIGN KEY (`session_id`) REFERENCES `learning_sessions` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `learning_messages` (
  `id` varchar(36) NOT NULL,
  `space_id` varchar(36) NOT NULL,
  `conversation_id` varchar(36) NOT NULL,
  `run_id` varchar(36) NOT NULL,
  `status` varchar(32) NOT NULL,
  `message` text NOT NULL,
  `sequence` int NOT NULL,
  `snapshot` json NOT NULL,
  `response` json DEFAULT NULL,
  `created_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_message_sequence` (`conversation_id`,`sequence`),
  KEY `ix_learning_messages_space_id` (`space_id`),
  KEY `ix_learning_messages_conversation_id` (`conversation_id`),
  CONSTRAINT `learning_messages_ibfk_1` FOREIGN KEY (`space_id`) REFERENCES `learning_spaces` (`id`),
  CONSTRAINT `learning_messages_ibfk_2` FOREIGN KEY (`conversation_id`) REFERENCES `learning_conversations` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 仅当上面的建表语句全部成功后，记录当前迁移版本。
INSERT INTO `alembic_version` (`version_num`) VALUES ('0013_material_raw');

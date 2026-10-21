-- =======================================================
-- ExamGuard AI - Supabase Cloud Database Schema
-- Run this in your Supabase Project -> SQL Editor
-- =======================================================

-- 1. Table: exam_reports (Stores candidate test records & violations)
CREATE TABLE IF NOT EXISTS exam_reports (
    id BIGSERIAL PRIMARY KEY,
    candidate_name TEXT NOT NULL,
    face_count INTEGER DEFAULT 0,
    violations INTEGER DEFAULT 0,
    evidence_count INTEGER DEFAULT 0,
    timestamp TIMESTAMPTZ DEFAULT NOW()
);

-- 2. Table: questions (Stores exam question bank)
CREATE TABLE IF NOT EXISTS questions (
    id BIGSERIAL PRIMARY KEY,
    question_text TEXT NOT NULL,
    option_a TEXT NOT NULL,
    option_b TEXT NOT NULL,
    option_c TEXT NOT NULL,
    option_d TEXT NOT NULL,
    correct_option INTEGER DEFAULT 0
);

-- 3. Default Questions (Optional Seed Data)
INSERT INTO questions (question_text, option_a, option_b, option_c, option_d, correct_option)
VALUES 
('Which protocol provides reliable transport in TCP/IP?', 'TCP', 'UDP', 'ICMP', 'ARP', 0),
('Which layer is responsible for routing?', 'Transport', 'Network', 'Session', 'Presentation', 1),
('What does DNS translate?', 'IP to MAC', 'Domain to IP', 'Port to IP', 'MAC to IP', 1),
('Which protocol is connectionless?', 'TCP', 'FTP', 'UDP', 'SMTP', 2),
('Which device operates at Layer 2?', 'Router', 'Switch', 'Firewall', 'Gateway', 1)
ON CONFLICT DO NOTHING;

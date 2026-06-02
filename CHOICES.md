# Key Architectural Choices

This document outlines three major architectural and design decisions made during the development of the Store Intelligence system. For each decision, we present the options considered, the AI's suggestion, and our final rationale.

## 1. Detection Model Selection

**The Goal:** Accurately detect people, track their movement across frames, and output bounding boxes fast enough to process 15fps CCTV footage efficiently.

**Options Considered:**
1. **MediaPipe Pose / Object Detection:** Very fast, but often struggles with the dense crowds and partial occlusions seen in the `billing_area.mp4` clip.
2. **RT-DETR:** High accuracy transformer-based model, but computationally heavy and harder to run without a dedicated GPU.
3. **YOLOv8 + ByteTrack:** The industry standard for real-time tracking. YOLOv8n is extremely fast, and ByteTrack handles occlusions well by associating low-confidence detection boxes rather than discarding them.

**AI Suggestion & Final Choice:**
We prompted an LLM to evaluate the trade-offs specifically for "overhead retail CCTV with frequent partial occlusions". The AI strongly suggested **YOLOv8 combined with ByteTrack**. It pointed out that ByteTrack's core innovation—keeping low-confidence boxes to sustain track identities during occlusion—is exactly what is needed for the "billing queue buildup" edge case described in the challenge.
We agreed and implemented YOLOv8n + ByteTrack. It proved fast enough to run locally and accurate enough to handle the store environments.

## 2. Event Schema Design

**The Goal:** Define a structured JSON schema that the detection layer emits and the API consumes. It must support complex downstream queries like funnel drop-offs and queue depth.

**Options Considered:**
1. **Deeply Nested Schema:** Grouping all events for a single visitor into one large JSON object. 
2. **Flat Schema with Metadata Block:** Emitting discrete, atomic events (`ENTRY`, `ZONE_DWELL`, etc.) with a flat root structure but a flexible `metadata` dictionary for context-specific data.

**AI Suggestion & Final Choice:**
We asked the AI to design a schema that would be easiest to ingest into a SQL database later. The AI recommended the **Flat Schema with Metadata Block**. It argued that a deeply nested schema makes real-time streaming impossible (you'd have to wait for the visitor to leave before emitting the record). A flat schema allows the detection pipeline to fire-and-forget events the millisecond they happen. 
We adopted this approach. The resulting schema (with fields like `event_id`, `visitor_id`, `event_type`, and `metadata: {"queue_depth": ...}`) mapped perfectly to a flat SQLAlchemy model with a JSON column for the metadata.

## 3. API Architecture & Persistence

**The Goal:** Build an Intelligence API that computes real-time metrics, funnels, and anomalies based on the event stream and POS data.

**Options Considered:**
1. **In-Memory Pandas DataFrames:** Read the `events.jsonl` file into a Pandas DataFrame and compute metrics on the fly. Fast to implement, but doesn't handle real-time appending easily and consumes massive memory.
2. **PostgreSQL + Redis:** A production-grade stack. Overkill for a local `docker compose up` deployment and introduces unnecessary complexity.
3. **FastAPI + SQLite (via SQLAlchemy):** FastAPI provides the async, high-performance web layer. SQLite provides a zero-setup, disk-backed SQL engine that supports JSON querying.

**AI Suggestion & Final Choice:**
We presented the requirement ("Must run via docker compose up. API must be production-aware") to the AI. The AI suggested the **FastAPI + SQLite** route. It explained that SQLite is surprisingly robust for gigabyte-scale data and eliminates the need for a separate database container in Docker, making the reviewer's job much easier while still demonstrating full ORM/SQL proficiency.
We went with this choice. It allowed us to build complex, highly optimized SQL queries for the `/funnel` and `/anomalies` endpoints without the overhead of managing a Postgres cluster.

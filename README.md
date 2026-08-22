# air-llm

A production-grade gateway that isolates AI applications from rapidly changing LLM APIs. Provides a unified interface, automatic failover, cost tracking, and observability across OpenAI, Anthropic, Gemini, Ollama, and any OpenAI-compatible backend. Designed for enterprise workloads requiring vendor independence, reliability, and auditability.

## Why This Exists

LLM providers frequently change:
- API shapes and SDKs
- Rate limits and pricing
- Model availability and SLAs
- Error codes and retry semantics

Embedding provider-specific logic directly into application code creates tight coupling and makes provider switches expensive and risky. This project implements the **air-llmr** gateway pattern to decouple application logic from volatile LLM provider SDKs and APIs.

## Key Capabilities

- **Unified API contract** — One consistent interface for all provider calls
- **Provider adapters** — Pluggable modules for OpenAI, Anthropic, Gemini, Ollama, vLLM, etc.
- **Intelligent routing** — Latency-based, cost-aware, and SLA-driven request distribution 
- **Automatic failover** — Circuit breakers and fallback chains for high availability 
- **Observability** — Request/response logging, token tracking, and cost attribution per provider 
- **Security** — Centralized secrets management and identity-based access control 

Ideal for teams building multi-agent systems, RAG pipelines, or any AI application that must survive provider switches without code changes. 

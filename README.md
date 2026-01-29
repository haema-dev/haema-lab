# haema's Home Lab

## 📊 Project Roadmap & Variance Analysis (Updated: 2026-01-29)

| Stage                        | Planned         | Actual               | Variance | Delay Analysis & Action Items                                      |
| :--------------------------- | :-------------- | :------------------- | :------: | :----------------------------------------------------------------- |
| **01. System Design**        | Jan 16 - Jan 19 | Jan 16 - Jan 19      |    0d    | **[Complete]** Architecture finalized for fixed hardware baseline. |
| **02. DevOps & Environment** | Jan 23 - Jan 24 | Jan 26 - Jan 29      |    5d    | **[Cause]** Changes to Kubernetes via Kubespray                    |
| **03. Model Evaluation**     | Jan 30 - Jan 31 | Jan 30 - **Ongoing** |    -     | -                                                                  |

> **Current Status:** 🏗️ Stage 03 (Model Evaluation) in progress.

---

## 🏛️ System Architecture

![System Architecture Diagram](./system_architecture.png)

## ⚙️ Infrastructure Constraints & Design Decisions

**Hardware Topology**:

- **Compute Node (A)**: 6-core CPU, 12 threads, 32GB RAM (Kubernetes control plane + application workloads)
- **Inference Node (B)**: 8-core CPU, 16 threads, 64GB RAM, iGPU
  (Model serving + local LLM inference [EXAONE-3.5-7.8B via Ollama])
- **Constraint**: Both nodes are fixed in specs; no cloud spillover for training.

**Cost-Sensitive Engineering**:

- **Local Processing**: EXAONE-3.5-7.8B INT4 handles summarization and RAG embeddings
- **API Generation**: Gemini API called only for final text generation
- **Result**: Significant cost savings vs. 100% cloud inference

## 🛠️ Technology Stack & Tooling

- **Virtualization**: Proxmox VE
- **Orchestration**: Kubernetes via Kubespray
- **Network & Access**: Tailscale (Mesh VPN & Remote Node Access)
- **CI/CD & GitOps**: GitHub Actions, ArgoCD
- **Monitoring**: Prometheus & Grafana Stack
- **MLOps & Serving**: [Ollama (local preprocessing: EXAONE-3.5-7.8B INT4) / Gemini API (remote generation)]

## 🎯 Key Challenges & Tasks

1. **Design and visualize** the overall service architecture to optimize limited local resources.
2. **Establish** a robust DevOps pipeline and environment setup for automated deployment.

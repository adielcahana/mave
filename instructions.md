DS Home Assignment:
Building a Problem Resolution System for HPC Logs
Objective
High performance computing (HPC) combines computing resources to achieve
performance beyond a single computer, using either custom supercomputers or
clusters of individual machines.
Your task is to build an intelligent system that not only analyzes HPC logs but creates an
end-to-end workow for automated problem resolution. The goal is to reduce downtime
by providing precise and actionable recommendations. The solution should be designed
to handle evolving log formats and recommendations, making it "future-proof." While
latency is not the primary concern, take into consideration that you want this to function
in a real-world environment where cost and latency are important.
Problem Statement
● Analyze the provided HPC logs dataset to identify errors.
● Recommend specic actions to address these errors.
● Note that new logs and recommendations are introduced occasionally, so the
system needs to be adaptable to new log templates, formats and recommended
actions that are introduced over time.
Dataset
● Download HPC dataset
● The dataset contains structured HPC logs with elds such as timestamps, event
IDs, and log messages.
● The logs may include dierent types of entries (e.g., normal operations,
warnings, and errors). Your focus should be on identifying errors and
providing actionable recommendations.
Core Requirements
Data Processing and Analysis
● Process structured HPC logs
● Describe how data ingestion will work (don’t implement, just diagram)
Model integration and agentic workow
● Develop a pipeline for log analysis. Note that there may be multiple stages, from
identifying the errors to providing recommendations, so be sure to use the right
tool for the job.
● You should include an agent that can
● Find errors and explain them
● Query knowledge sources (for example - documentation, external
APIs)
● Suggest remediation steps, back them up with citations and reasons.
Note that you can register for access to LLMs at build.nvidia.com, but you can also run
them locally or via another API service.
Submission
A link to a github repository including the les:
● report, A text/pdf/Markdown le including a detailed description of your thought
process, your solution and suggested future improvements. ● README.md, a
Markdown le including exact and precise instructions to build and execute your tool
● Any other source les needed to build
Please invite guy@mave.com to the repository.
"""
merge_and_train.py
Merge Q&A data with deployment data, then fine-tune LLaMA
"""
import json, os, subprocess, sys

# ── Step 1: Generate Q&A data ──────────────────────────────
print("Step 1: Generating Q&A training data...")
os.system("python3 generate_qa_data.py")

# ── Step 2: Merge datasets ─────────────────────────────────
print("\nStep 2: Merging datasets...")

deploy_path = "dataset/finetune_samples.jsonl"
qa_path     = "dataset/k8s_qa_samples.jsonl"
merged_path = "dataset/merged_samples.jsonl"

deploy_data = []
qa_data     = []

if os.path.exists(deploy_path):
    with open(deploy_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                deploy_data.append(json.loads(line))

if os.path.exists(qa_path):
    with open(qa_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                qa_data.append(json.loads(line))

merged = deploy_data + qa_data

with open(merged_path, "w", encoding="utf-8") as f:
    for item in merged:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")

print(f"Deployment data: {len(deploy_data)} samples")
print(f"Q&A data: {len(qa_data)} samples")
print(f"Merged total: {len(merged)} samples")
print(f"Saved to: {merged_path}")

# ── Step 3: Fine-tune ──────────────────────────────────────
print("\nStep 3: Starting fine-tune with merged data...")
print("This will take ~15-20 minutes on RTX 4090...")

# Patch train_local.py to use merged dataset temporarily
train_content = open("train_local.py").read()
patched = train_content.replace(
    'finetune_samples.jsonl',
    'merged_samples.jsonl'
)
with open("train_local_merged.py", "w") as f:
    f.write(patched)

print("Running train_local_merged.py...")
result = subprocess.run(
    [sys.executable, "train_local_merged.py"],
    capture_output=False
)

if result.returncode == 0:
    print("\nFine-tuning complete!")
    print("Restart model_server.py to load the new weights.")
else:
    print(f"\nFine-tuning failed with code {result.returncode}")

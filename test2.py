import re
import json
import llama_client

text = '{"pods": 3, "image": "nginx:latest", "port": null, "memory": null}'
print("before:", text)

result = llama_client._repair_json(text)
print("repair result:", result)

valid = llama_client._validate(result) if result else False
print("validate:", valid)

import llama_client

result = llama_client.ask_llama("deploy 3 pods of nginx:latest")
print("ask_llama result:", result)

import json
text = '{"pods": 3, "image": "nginx:latest", "app_name": "user-service", "port": "__NULL__", "memory": "__NULL__"}'
result = json.loads(text)
result = {k: v for k, v in result.items() if v != "__NULL__"}
print("final:", result)
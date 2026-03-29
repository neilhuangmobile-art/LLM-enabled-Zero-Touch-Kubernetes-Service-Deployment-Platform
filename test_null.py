import re, json

text = '{"pods": 3, "image": "nginx:latest", "port": null, "memory": null}'
text = re.sub(r':\s*null', ': "__REMOVE__"', text)
result = json.loads(text)
result = {k: v for k, v in result.items() if v != '__REMOVE__'}
print(result)
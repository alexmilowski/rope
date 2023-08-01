import sys
from string import Template

if len(sys.argv)<2:
   sys.exit(1)

try:
   with open(sys.argv[1]) as raw:
      template = raw.read()
except FileNotFoundError as ex:
   print(f'Cannot open file {sys.argv[1]}',file=sys.stderr)
   sys.exit(1)

data = {}
for mapping in sys.argv[2:]:
   name, _, value = mapping.partition('=')
   data[name] = value

try:
   print(template.format(**data))
except KeyError as ex:
   print(f'Missing parameter {ex}',file=sys.stderr)

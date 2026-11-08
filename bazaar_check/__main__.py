import sys

from .game import make_output_safe
from .runner import main

make_output_safe()
sys.exit(main(sys.argv[1:]))

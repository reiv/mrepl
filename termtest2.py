# Original code: http://stackoverflow.com/a/4653306

import time, readline, threading
import sys

_input = input
_print = print

INPUT = False
PROMPT = ''

_lock = threading.Lock()

def input(s):
    global INPUT, PROMPT
    with _lock:
        INPUT = True
        PROMPT = s    
    result = _input(s)
    with _lock:
        INPUT = False
        PROMPT = ''
    return result
    
def print(s):
    with _lock:
        if INPUT:
            sys.stdout.write('\r'+' '*(len(readline.get_line_buffer())+len(PROMPT))+'\r')
            _print(s)
            sys.stdout.write(PROMPT + readline.get_line_buffer())
            # Needed or text doesn't show until a key is pressed
            sys.stdout.flush()
        else:
            _print(s)


__all__ = ['print', 'input']

# DEMO:

if __name__ == '__main__':
    def noisy_thread():
        while True:
            try:
                time.sleep(3)
            except KeyboardInterrupt:
                break
            print('Interrupting text!')

    def input_thread():
        while True:
            try:
                s = input('Input: ')
            except KeyboardInterrupt:
                break

    # NB: terminal seems to be left in a broken state if input() is used
    # anywhere but in the main thread.
    threading.Thread(target=noisy_thread).start()

    input_thread()

"""Keep provider reasoning out of user-visible answers and saved chat history."""
import re


class AnswerStream:
    tags = ('<think>', '</think>', '<analysis>', '</analysis>')

    def __init__(self, initial_hidden=False):
        self.buffer = ''
        self.hidden = initial_hidden

    def feed(self, text):
        self.buffer += text
        output = ''
        while self.buffer:
            lower = self.buffer.lower()
            found = [(lower.find(tag),tag) for tag in self.tags if tag in lower]
            if found:
                index,tag = min(found)
                if not self.hidden:
                    output += self.buffer[:index]
                self.hidden = not tag.startswith('</')
                self.buffer = self.buffer[index+len(tag):]
                continue
            keep = max((n for tag in self.tags for n in range(1,len(tag)) if lower.endswith(tag[:n])), default=0)
            end = len(self.buffer)-keep
            if not self.hidden:
                output += self.buffer[:end]
            self.buffer = self.buffer[end:]
            break
        return output

    def finish(self):
        output = '' if self.hidden else self.buffer
        # A partial opening tag at disconnect is never a visible answer.
        if any(tag.startswith(output.lower()) for tag in self.tags) and output.startswith('<'):
            output = ''
        self.buffer = ''
        return output


def clean_answer(text):
    # Older Qwen templates can include only the closing marker in content.
    close = re.search(r'</(?:think|analysis)>',text,re.I)
    if close and not re.search(r'<(?:think|analysis)>',text[:close.start()],re.I):
        text = text[close.end():]
    stream = AnswerStream()
    return (stream.feed(text)+stream.finish()).strip()

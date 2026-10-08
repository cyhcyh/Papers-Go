"""Extract visible answer strings while a single JSON response is arriving."""
import json

ANSWER_KEYS=('problem','related_work','method','evaluation','future','summary')


class ReadingJSONStream:
    def __init__(self):
        self.stack=[]
        self.in_string=False
        self.key_string=False
        self.path=()
        self.string=[]
        self.escape=''
        self.high_surrogate=None
        self.answers={key:'' for key in ANSWER_KEYS}
        self.completed_answers=set()
        self.paper_kind='mixed'

    def value_path(self):
        if not self.stack:
            return ()
        top=self.stack[-1]
        return top['path']+(top.get('key'),)

    def consume_value(self):
        if self.stack:
            self.stack[-1]['state']='comma'

    def emit(self, char, pending):
        self.string.append(char)
        if len(self.path)==2 and self.path[0]=='answers' and self.path[1] in pending:
            pending[self.path[1]].append(char)

    def unicode_char(self, code, pending):
        if self.high_surrogate is not None:
            high=self.high_surrogate
            self.high_surrogate=None
            if 0xDC00<=code<=0xDFFF:
                self.emit(chr(0x10000+((high-0xD800)<<10)+(code-0xDC00)),pending)
                return
            self.emit('\ufffd',pending)
        if 0xD800<=code<=0xDBFF:
            self.high_surrogate=code
        else:
            self.emit(chr(code) if not 0xDC00<=code<=0xDFFF else '\ufffd',pending)

    def feed(self, text):
        completed_before=len(self.completed_answers)
        pending={key:[] for key in ANSWER_KEYS}
        for char in text:
            if self.in_string:
                if self.escape.startswith('u'):
                    self.escape+=char
                    if len(self.escape)==5:
                        self.unicode_char(int(self.escape[1:],16),pending)
                        self.escape=''
                elif self.escape=='slash':
                    if char=='u':
                        self.escape='u'
                    else:
                        self.emit(json.loads('"\\'+char+'"'),pending)
                        self.escape=''
                elif char=='\\':
                    self.escape='slash'
                elif char=='"':
                    self.in_string=False
                    value=''.join(self.string)
                    if self.key_string:
                        self.stack[-1].update(key=value,state='colon')
                    else:
                        if len(self.path)==2 and self.path[0]=='answers' and self.path[1] in self.answers and value.strip():
                            self.completed_answers.add(self.path[1])
                        if self.path==('paper_kind',) and value in ('empirical','theoretical','mixed'):
                            self.paper_kind=value
                        self.consume_value()
                    self.string=[]
                else:
                    self.emit(char,pending)
                continue
            if char.isspace():
                continue
            if char in '{[':
                path=self.value_path()
                self.consume_value()
                self.stack.append({'path':path,'kind':char,'state':'key' if char=='{' else 'value','key':None})
            elif char=='"' and self.stack:
                self.in_string=True
                self.key_string=self.stack[-1]['kind']=='{' and self.stack[-1]['state']=='key'
                self.path=() if self.key_string else self.value_path()
                self.string=[]
            elif char in '}]' and self.stack:
                self.stack.pop()
            elif char==':' and self.stack:
                self.stack[-1]['state']='value'
            elif char==',' and self.stack:
                self.stack[-1]['state']='key' if self.stack[-1]['kind']=='{' else 'value'
            elif self.stack and self.stack[-1]['state']=='value':
                self.consume_value()
        changed=len(self.completed_answers)!=completed_before
        for key,chars in pending.items():
            if chars:
                self.answers[key]+=''.join(chars)
                changed=True
        return changed

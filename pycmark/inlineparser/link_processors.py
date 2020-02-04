"""
    pycmark.inlineparser.link_processors
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    Link processor classes for InlineParser.

    :copyright: Copyright 2017-2019 by Takeshi KOMIYA
    :license: Apache License 2.0, see LICENSE for details.
"""

import re
from typing import Generator, Tuple

from docutils import nodes
from docutils.nodes import Element, Text

from pycmark import addnodes
from pycmark.inlineparser import PatternInlineProcessor, backtrack_onerror
from pycmark.readers import TextReader
from pycmark.utils import entitytrans, normalize_uri
from pycmark.utils import (
    ESCAPED_CHARS, escaped_chars_pattern, get_root_document, normalize_link_label, unescape, transplant_nodes
)


class LabelNotMatched(Exception):
    pass


# 6.5 Links
# 6.6 Images
class LinkOpenerProcessor(PatternInlineProcessor):
    pattern = re.compile(r'\!?\[')

    def run(self, reader: TextReader, document: Element) -> bool:
        marker = reader.consume(self.pattern).group(0)
        document += addnodes.bracket(marker=marker, can_open=True, active=True, position=reader.position)
        return True


class LinkCloserProcessorBase(PatternInlineProcessor):
    def get_opening_brackets(self, document: Element) -> Generator[addnodes.bracket, None, None]:
        for node in document:
            if isinstance(node, addnodes.bracket) and node['can_open']:
                yield node

    def get_last_opening_brackets(self, document: Element) -> addnodes.bracket:
        openers = list(self.get_opening_brackets(document))
        if openers:
            return openers[-1]
        else:
            return None

    def create_node(self, title: str, destination: str, document: Element, opener: addnodes.bracket) -> Element:
        node: Element = None
        if opener['marker'] == '![':
            from pycmark.transforms import EmphasisConverter  # lazy loading
            para = transplant_nodes(document, nodes.paragraph(), start=opener)
            EmphasisConverter(para).apply()
            node = nodes.image('', uri=destination, alt=para.astext())
            if title:
                node['title'] = title
        else:
            node = nodes.reference('', refuri=destination)
            transplant_nodes(document, node, start=opener)
            if title:
                node['reftitle'] = title

            # deactivate all left brackets before the link
            for n in self.get_opening_brackets(document):
                if n['marker'] == '[':
                    n['active'] = False

        return node

    def lookup_target(self, node: Element, refname: str) -> Element:
        document = get_root_document(node)

        refname = normalize_link_label(refname)
        node_id = document.nameids.get(refname)
        if node_id is None:
            raise LabelNotMatched

        return document.ids.get(node_id)


class UnmatchedLinkCloserProcessor(LinkCloserProcessorBase):
    pattern = re.compile(r'\]')
    priority = 100

    def run(self, reader: TextReader, document: Element) -> bool:
        opener = self.get_last_opening_brackets(document)
        if opener is None:
            reader.step(1)
            document += Text(']')
            return True
        elif not opener['active']:
            reader.step(1)
            opener.replace_self(Text(opener['marker']))
            document += Text(']')
            return True
        else:
            # pass to other LinkCloserProcessors
            return False


class LinkCloserWithDestinationProcessor(LinkCloserProcessorBase):
    # link destination + link title (optional)
    #     [...](<.+> ".+")
    #     [...](.+ ".+")
    pattern = re.compile(r'\]\(')
    priority = 400

    @backtrack_onerror
    def run(self, reader: TextReader, document: Element) -> bool:
        opener = self.get_last_opening_brackets(document)
        if opener is None:
            return False

        reader.consume(self.pattern)
        destination, title = self.parse_link_destination(reader, document)

        document += self.create_node(title, destination, document, opener)
        document.remove(opener)
        return True

    def parse_link_destination(self, reader: TextReader, document: Element) -> Tuple[str, str]:
        destination = LinkDestinationParser().parse(reader, document)
        title = LinkTitleParser().parse(reader, document)
        assert reader.consume(re.compile(r'\s*\)'))

        return destination, title


class LinkCloserWithLabelProcessor(LinkCloserProcessorBase):
    # link label
    #     [...][.+]
    #     [...][]
    pattern = re.compile(r'\]\[')
    priority = 400

    @backtrack_onerror
    def run(self, reader: TextReader, document: Element) -> bool:
        opener = self.get_last_opening_brackets(document)
        if opener is None:
            return False

        try:
            reader.consume(self.pattern)
            destination, title = self.parse_link_label(reader, document, opener)
            document += self.create_node(title, destination, document, opener)
            document.remove(opener)
            return True
        except LabelNotMatched:
            # deactivate brackets because no trailing link destination or link-label
            opener.replace_self(Text(opener['marker']))
            raise

    def parse_link_label(self, reader: TextReader, document: Element, opener: Element = None) -> Tuple[str, str]:  # NOQA
        refname = LinkLabelParser().parse(reader, document)
        if refname == '':
            # collapsed reference link
            #     [...][]
            refname = reader[opener['position']:reader.position - 2]

        target = self.lookup_target(document, refname)
        return target.get('refuri'), target.get('title')


class LinkCloserProcessor(LinkCloserProcessorBase):
    # shortcut reference link
    #    [...]
    pattern = re.compile(r'\]')

    @backtrack_onerror
    def run(self, reader: TextReader, document: Element) -> bool:
        opener = self.get_last_opening_brackets(document)
        if opener is None:
            return False

        try:
            reader.consume(self.pattern)
            destination, title = self.process_link_or_image(reader, document, opener)
            document += self.create_node(title, destination, document, opener)
            document.remove(opener)
            return True
        except LabelNotMatched:
            # deactivate brackets because no trailing link destination or link-label
            opener.replace_self(Text(opener['marker']))
            raise

    def process_link_or_image(self, reader: TextReader, document: Element, opener: addnodes.bracket) -> Tuple[str, str]:  # NOQA
        try:
            refid = reader[opener['position']:reader.position - 1]
            target = self.lookup_target(document, refid)
            return target.get('refuri'), target.get('title')
        except LabelNotMatched:
            # deactivate brackets because no trailing link destination or link-label
            opener.replace_self(Text(opener['marker']))
            raise


class LinkDestinationParser:
    pattern = re.compile(r'\s*<((?:[^<>\n\\]|' + ESCAPED_CHARS + r')*)>', re.S)

    def parse(self, reader: TextReader, document: Element) -> str:
        if re.match(r'^\s*<', reader.remain):
            matched = reader.consume(self.pattern)
            if not matched:
                return ''
            else:
                return self.normalize_link_destination(matched.group(1))
        else:
            return self.parseBareLinkDestination(reader, document)

    def normalize_link_destination(self, s: str) -> str:
        s = entitytrans._unescape(s)
        s = unescape(s)
        s = normalize_uri(s)
        return s

    def parseBareLinkDestination(self, reader: TextReader, document: Element) -> str:
        assert reader.consume(re.compile(r'[ \n]*'))

        if reader.remain == '':  # must be empty line!
            return None

        parens = 0
        start = reader.position
        while reader.remain:
            c = reader.remain[0]
            if c in (' ', '\n'):
                break
            elif c == '(':
                parens += 1
            elif c == ')':
                parens -= 1
                if parens < 0:
                    break
            elif escaped_chars_pattern.match(reader.remain):
                reader.step()  # one more step for escaping

            reader.step()

        end = reader.position
        return self.normalize_link_destination(reader[start:end])


class LinkTitleParser:
    pattern = re.compile(r'\s*("(' + ESCAPED_CHARS + r'|[^"])*"|' +
                         r"'(" + ESCAPED_CHARS + r"|[^'])*'|" +
                         r"\((" + ESCAPED_CHARS + r"|[^)])*\))")

    def parse(self, reader: TextReader, document: Element) -> str:
        matched = reader.consume(self.pattern)
        if matched:
            return unescape(entitytrans._unescape(matched.group(1)[1:-1]))
        else:
            return None


class LinkLabelParser:
    pattern = re.compile(r'(?:[^\[\]\\]|' + ESCAPED_CHARS + r'|\\){0,1000}\]')

    def parse(self, reader: TextReader, document: Element) -> str:
        matched = reader.consume(self.pattern)
        if matched:
            return matched.group(0)[:-1]
        else:
            return None

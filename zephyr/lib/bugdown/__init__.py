import markdown
import logging
import traceback
import urlparse
import re
import os.path
import glob

from django.core import mail

from zephyr.lib.avatar  import gravatar_hash
from zephyr.lib.bugdown import codehilite, fenced_code
from zephyr.lib.bugdown.fenced_code import FENCE_RE
from zephyr.lib.timeout import timeout

class InlineImagePreviewProcessor(markdown.treeprocessors.Treeprocessor):
    def is_image(self, url):
        # List from http://support.google.com/chromeos/bin/answer.py?hl=en&answer=183093
        for ext in [".bmp", ".gif", ".jpg", "jpeg", ".png", ".webp"]:
            if url.lower().endswith(ext):
                return True
        return False

    def dropbox_image(self, url):
        if not self.is_image(url):
            return None
        parsed_url = urlparse.urlparse(url)
        if (parsed_url.netloc == 'dropbox.com' or parsed_url.netloc.endswith('.dropbox.com')) \
                and parsed_url.path.startswith('/s/'):
            return "%s?dl=1" % (url,)
        return None

    def youtube_image(self, url):
        # Youtube video id extraction regular expression from http://pastebin.com/KyKAFv1s
        # If it matches, match.group(2) is the video id.
        youtube_re = r'^((?:https?://)?(?:youtu\.be/|(?:\w+\.)?youtube(?:-nocookie)?\.com/)(?:(?:(?:v|embed)/)|(?:(?:watch(?:_popup)?(?:\.php)?)?(?:\?|#!?)(?:.+&)?v=)))?([0-9A-Za-z_-]+)(?(1).+)?$'
        match = re.match(youtube_re, url)
        if match is None:
            return None
        return "http://i.ytimg.com/vi/%s/default.jpg" % (match.group(2),)

    # Search the tree for <a> tags and read their href values
    def find_images(self, root):
        images = []
        stack = [root]

        while stack:
            currElement = stack.pop()
            for child in currElement.getchildren():
                if child.getchildren():
                    stack.append(child)

                if child.tag == "a":
                    url = child.get("href")
                    youtube = self.youtube_image(url)
                    if youtube is not None:
                        images.append((youtube, url))
                        continue
                    dropbox = self.dropbox_image(url)
                    if dropbox is not None:
                        images.append((dropbox, url))
                        continue
                    if self.is_image(url):
                        images.append((url, url))
                        continue
        return images

    def run(self, root):
        image_urls = self.find_images(root)
        for (url, link) in image_urls:
            a = markdown.util.etree.SubElement(root, "a")
            a.set("href", link)
            a.set("target", "_blank")
            a.set("title", link)
            img = markdown.util.etree.SubElement(a, "img")
            img.set("src", url)
            img.set("class", "message_inline_image")

        return root

class Gravatar(markdown.inlinepatterns.Pattern):
    def handleMatch(self, match):
        img = markdown.util.etree.Element('img')
        img.set('class', 'message_body_gravatar img-rounded')
        img.set('src', 'https://secure.gravatar.com/avatar/%s?d=identicon&s=30'
            % (gravatar_hash(match.group('email')),))
        return img

# We first map syntax of the form ":)" to the ":smile:" syntax, and
# then map syntax of the form ":foo:" to the emoji named "foo".
smiley_to_emoji = {
    # We're somewhat restrained here, to avoid false positives
    ":-)" : "blush",  # This is a very weird coopting of the blush emoticon, but the normal
    ":)"  : "blush",  # smile was so smiley that it became indistinguishable from grin.
    ":-(" : "worried",
    ":("  : "worried",
    ";-)" : "wink",
    ";)"  : "wink",
    ":-P" : "stuck_out_tongue",
    ":-p" : "stuck_out_tongue",
    ":P"  : "stuck_out_tongue",
    ":p"  : "stuck_out_tongue",
    ":-*" : "kissing_closed_eyes",
    ":*"  : "kissing_closed_eyes",
    "8-)" : "sunglasses",
    "8)"  : "sunglasses",
    "O:-)": "innocent",
    "O:)" : "innocent",
    "o:-)": "innocent",
    "o:)" : "innocent",
    ":-/" : "confused",
    ":/"  : "confused",
    ":'(" : "cry",
    ":-D" : "smiley",
    ":D"  : "smiley",
    ":-|" : "expressionless",
    ":|"  : "expressionless",
    "<3"  : "heart",
    }
smiley_regex = '|'.join([re.escape(face) for face in smiley_to_emoji])

path_to_emoji = os.path.join(os.path.dirname(__file__), '..', '..',
                             # This should be zephyr/
                             'static', 'third', 'gemoji', 'images', 'emoji', '*.png')
emoji_list = [os.path.splitext(os.path.basename(fn))[0] for fn in glob.glob(path_to_emoji)]

def make_emoji(emoji_name, display_string):
    elt = markdown.util.etree.Element('img')
    elt.set('src', 'static/third/gemoji/images/emoji/%s.png' % (emoji_name,))
    elt.set('class', 'emoji')
    elt.set("alt", display_string)
    elt.set("title", display_string)
    return elt

class Emoji(markdown.inlinepatterns.Pattern):
    def handleMatch(self, match):
        orig_syntax = match.group("syntax")
        name = orig_syntax[1:-1]
        if name not in emoji_list:
            return orig_syntax
        return make_emoji(name, orig_syntax)

class Smiley(markdown.inlinepatterns.Pattern):
    def handleMatch(self, match):
        orig_syntax = match.group("syntax")
        if orig_syntax not in smiley_to_emoji:
            return orig_syntax
        return make_emoji(smiley_to_emoji[orig_syntax], orig_syntax)

def fixup_link(link):
    """Set certain attributes we want on every link."""
    link.set('target', '_blank')
    link.set('title',  link.get('href'))


def sanitize_url(url):
    """
    Sanitize a url against xss attacks.
    See the docstring on markdown.inlinepatterns.LinkPattern.sanitize_url.
    """
    try:
        parts = urlparse.urlparse(url.replace(' ', '%20'))
        scheme, netloc, path, params, query, fragment = parts
    except ValueError:
        # Bad url - so bad it couldn't be parsed.
        return ''

    # Humbug modification: If scheme is not specified, assume http://
    # It's unlikely that users want relative links within humbughq.com.
    # We re-enter sanitize_url because netloc etc. need to be re-parsed.
    if not scheme:
        return sanitize_url('http://' + url)

    locless_schemes = ['', 'mailto', 'news']
    if netloc == '' and scheme not in locless_schemes:
        # This fails regardless of anything else.
        # Return immediately to save additional proccessing
        return None

    for part in parts[2:]:
        if ":" in part:
            # Not a safe url
            return None

    # Url passes all tests. Return url as-is.
    return urlparse.urlunparse(parts)

def url_to_a(url):
    a = markdown.util.etree.Element('a')
    if '@' in url:
        href = 'mailto:' + url
    else:
        href = url

    href = sanitize_url(href)
    if href is None:
        # Rejected by sanitize_url; render it as plain text.
        return url

    a.set('href', href)
    a.text = url
    fixup_link(a)
    return a

class AutoLink(markdown.inlinepatterns.Pattern):
    def handleMatch(self, match):
        url = match.group('url')
        # As this will also match already-matched https?:// links,
        # don't doubly-link them
        if url[:5] == 'http:' or url[:6] == 'https:':
            return url
        return url_to_a(url)

class HttpLink(markdown.inlinepatterns.Pattern):
    def handleMatch(self, match):
        url = match.group('url')
        return url_to_a(url)

class UListProcessor(markdown.blockprocessors.OListProcessor):
    """ Process unordered list blocks.

        Based on markdown.blockprocessors.UListProcessor, but does not accept
        '+' or '-' as a bullet character."""

    TAG = 'ul'
    RE = re.compile(r'^[ ]{0,3}[*][ ]+(.*)')

class BugdownUListPreprocessor(markdown.preprocessors.Preprocessor):
    """ Allows unordered list blocks that come directly after a
        paragraph to be rendered as an unordered list

        Detects paragraphs that have a matching list item that comes
        directly after a line of text, and inserts a newline between
        to satisfy Markdown"""

    LI_RE = re.compile(r'^[ ]{0,3}[*][ ]+(.*)', re.MULTILINE)
    HANGING_ULIST_RE = re.compile(r'^.+\n([ ]{0,3}[*][ ]+.*)', re.MULTILINE)

    def run(self, lines):
        """ Insert a newline between a paragraph and ulist if missing """
        inserts = 0
        fence = None
        copy = lines[:]
        for i in xrange(len(lines) - 1):
            # Ignore anything that is inside a fenced code block
            m = FENCE_RE.match(lines[i])
            if not fence and m:
                fence = m.group('fence')
            elif fence and m and fence == m.group('fence'):
                fence = None

            # If we're not in a fenced block and we detect an upcoming list
            #  hanging off a paragraph, add a newline
            if not fence and lines[i] and \
                self.LI_RE.match(lines[i+1]) and not self.LI_RE.match(lines[i]):
                copy.insert(i+inserts+1, '')
                inserts += 1
        return copy

# Based on markdown.inlinepatterns.LinkPattern
class LinkPattern(markdown.inlinepatterns.Pattern):
    """ Return a link element from the given match. """
    def handleMatch(self, m):
        # Return the original link syntax as plain text,
        # if the link fails checks.
        orig_syntax = m.group(0)

        href = m.group(9)
        if not href:
            return orig_syntax

        if href[0] == "<":
            href = href[1:-1]
        href = sanitize_url(self.unescape(href.strip()))
        if href is None:
            return orig_syntax

        el = markdown.util.etree.Element('a')
        el.text = m.group(2)
        el.set('href', href)
        fixup_link(el)
        return el

class Bugdown(markdown.Extension):
    def extendMarkdown(self, md, md_globals):
        del md.preprocessors['reference']

        for k in ('image_link', 'image_reference', 'automail',
                  'autolink', 'link', 'reference', 'short_reference',
                  'escape', 'strong_em', 'emphasis', 'emphasis2',
                  'strong'):
            del md.inlinePatterns[k]

        # Custom bold syntax: **foo** but not __foo__
        md.inlinePatterns.add('strong',
            markdown.inlinepatterns.SimpleTagPattern(r'(\*\*)([^\n]+?)\2', 'strong'),
            '>not_strong')

        for k in ('hashheader', 'setextheader', 'olist', 'ulist'):
            del md.parser.blockprocessors[k]

        md.parser.blockprocessors.add('ulist', UListProcessor(md.parser), '>hr')

        md.inlinePatterns.add('gravatar', Gravatar(r'!gravatar\((?P<email>[^)]*)\)'), '_begin')
        md.inlinePatterns.add('emoji', Emoji(r'(?<!\S)(?P<syntax>:[^:\s]+:)(?!\S)'), '_begin')
        md.inlinePatterns.add('smileys', Smiley(r'(?<!\S)(?P<syntax>' + smiley_regex + r')(?!\S)'), '_begin')
        md.inlinePatterns.add('link', LinkPattern(markdown.inlinepatterns.LINK_RE, md), '>backtick')

        # markdown.inlinepatterns.Pattern compiles this with re.UNICODE, which
        # is important because we're using \w.
        #
        # This rule must come after the built-in 'link' markdown linkifier to
        # avoid errors.
        http_link_regex = r'\b(?P<url>https?://[^\s]+?)(?=[^\w/]*(\s|\Z))'
        md.inlinePatterns.add('http_autolink', HttpLink(http_link_regex), '>link')

        # A link starts at a word boundary, and ends at space, punctuation, or end-of-input.
        #
        # We detect a url by checking for the TLD, and building around it.
        #
        # To support () in urls but not match ending ) when a url is inside a parenthesis,
        # we match at maximum one set of matching parens in a url. We could extend this
        # to match two parenthetical groups, at the cost of more regex complexity.
        #
        # This rule must come after the http_autolink rule we add above to avoid double
        # linkifying.
        tlds = '|'.join(['co.uk', 'com', 'co', 'biz', 'gd', 'org', 'net', 'ly', 'edu', 'mil',
                         'gov', 'info', 'me', 'it', '.ca', 'tv', 'fm', 'io', 'gl'])
        link_regex = r"\b(?P<url>[^\s]+\.(%s)(?:/[^\s()\":]*?|([^\s()\":]*\([^\s()\":]*\)[^\s()\":]*))?)(?=([:;\?\),\.\'\"]\Z|[:;\?\),\.\'\"]\s|\Z|\s))" % (tlds,)
        md.inlinePatterns.add('autolink', AutoLink(link_regex), '>http_autolink')

        md.preprocessors.add('hanging_ulists',
                                 BugdownUListPreprocessor(md),
                                 "_begin")

        md.treeprocessors.add("inline_images", InlineImagePreviewProcessor(md), "_end")

_md_engine = markdown.Markdown(
    safe_mode     = 'escape',
    output_format = 'html',
    extensions    = ['nl2br',
        codehilite.makeExtension(configs=[
            ('force_linenos', False),
            ('guess_lang',    False)]),
        fenced_code.makeExtension(),
        Bugdown()])

# We want to log Markdown parser failures, but shouldn't log the actual input
# message for privacy reasons.  The compromise is to replace all alphanumeric
# characters with 'x'.
#
# We also use repr() to improve reproducibility, and to escape terminal control
# codes, which can do surprisingly nasty things.
_privacy_re = re.compile(r'\w', flags=re.UNICODE)
def _sanitize_for_log(md):
    return repr(_privacy_re.sub('x', md))

def convert(md):
    """Convert Markdown to HTML, with Humbug-specific settings and hacks."""

    # Reset the parser; otherwise it will get slower over time.
    _md_engine.reset()

    try:
        # Spend at most 5 seconds rendering.
        # Sometimes Python-Markdown is really slow; see
        # https://trac.humbughq.com/ticket/345
        html = timeout(5, _md_engine.convert, md)
    except:
        from zephyr.models import Recipient
        from zephyr.lib.actions import internal_send_message

        cleaned = _sanitize_for_log(md)

        html = '<p>[Humbug note: Sorry, we could not understand the formatting of your message]</p>'

        # Output error to log as well as sending a humbug and email
        logging.getLogger('').error('Exception in Markdown parser: %sInput (sanitized) was: %s'
            % (traceback.format_exc(), cleaned))
        subject = "Markdown parser failure"
        internal_send_message("humbug+errors@humbughq.com",
                Recipient.STREAM, "devel", subject,
                "Markdown parser failed, message sent to devel@")
        mail.mail_admins(subject, "Failed message: %s\n\n%s\n\n" % (
                                    cleaned, traceback.format_exc()),
                         fail_silently=False)

    return html

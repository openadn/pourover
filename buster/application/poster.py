from collections import defaultdict
import json
import logging
import urllib
from urlparse import urlparse, parse_qs

from bs4 import BeautifulSoup
from django.utils.encoding import iri_to_uri
from fnl.nlp import sentencesplitter as splitter
from google.appengine.ext import ndb
from google.appengine.api.images import Image, NotImageError
from lxml.html.clean import Cleaner


from constants import FORMAT_MODE
from utils import (append_query_string, strip_html_tags, ellipse_text, get_language,
                   guid_for_item, dict_hash)


logger = logging.getLogger(__name__)

MAX_CHARS = 256

cleaner = Cleaner(allow_tags=['a', 'p', 'ul', 'li', 'ol', 'span', 'b', 'strong'], add_nofollow=True, remove_unknown_tags=False)
clean_html = cleaner.clean_html


def parse_style_tag(text):
    if not text:
        return text
    text = text.strip()
    attrs = text.split(';')
    attrs = filter(None, map(lambda x: x.strip(), attrs))
    attrs = map(lambda x: x.split(':'), attrs)
    # logger.info('attrs: %s', attrs)
    return {x[0].strip(): x[1].strip() for x in attrs}


def find_video_src_url(summary):
    if not summary:
        return None, None

    soup = BeautifulSoup(summary)

    possible_embeds = soup.findAll('iframe')
    possible_embeds += soup.findAll('embed')

    for embed in possible_embeds:
        src_url = embed.get('src')
        urlparts = urlparse(src_url)
        if urlparts.netloc.endswith('youtube.com'):
            return src_url, 'youtube'

        if urlparts.netloc.endswith('vimeo.com'):
            return src_url, 'vimeo'

    return None, None


# http://stackoverflow.com/questions/4356538/how-can-i-extract-video-id-from-youtubes-link-in-python
def normalize_youtube_link(url):
    """
    Examples:
    - http://youtu.be/SA2iWivDJiE
    - http://www.youtube.com/watch?v=_oPAwA_Udwc&feature=feedu
    - http://www.youtube.com/embed/SA2iWivDJiE
    - http://www.youtube.com/v/SA2iWivDJiE?version=3&amp;hl=en_US
    """
    video_id = None
    query = urlparse(url)
    if query.hostname == 'youtu.be':
        video_id = query.path[1:]
    if query.hostname in ('www.youtube.com', 'youtube.com'):
        if query.path == '/watch':
            p = parse_qs(query.query)
            video_id = p['v'][0]
        if query.path[:7] == '/embed/':
            video_id = query.path.split('/')[2]
        if query.path[:3] == '/v/':
            video_id = query.path.split('/')[2]
    # fail?
    return 'http://www.youtube.com/watch?v=%s' % (video_id)


OEMBED_ENDPOINTS = {
    'vimeo': 'http://vimeo.com/api/oembed.json',
    'youtube': 'http://www.youtube.com/oembed',
}


@ndb.tasklet
def find_video_oembed(content):
    url, oembed_provider = find_video_src_url(content)
    if not url or not oembed_provider:
        return

    if url.startswith('//'):
        url = 'http:' + url

    if oembed_provider == 'youtube':
        url = normalize_youtube_link(url)

    if not url:
        return

    params = {
        'url': url
    }

    query_string = urllib.urlencode(params)
    ctx = ndb.get_context()
    resp = yield ctx.urlfetch(url='%s?%s' % (OEMBED_ENDPOINTS[oembed_provider], query_string), method='GET')
    # logger.info('Trying to fetch oembed data url:%s status_code:%s', url, resp.status_code)
    if resp.status_code != 200:
        raise ndb.Return(None)
    # logger.info('Found video provider:%s url:%s embed:%s', oembed_provider, url, json.loads(resp.content))
    raise ndb.Return(json.loads(resp.content))


def build_html_from_post(post):

    def entity_text(e):
        return post['text'][e['pos']:e['pos'] + e['len']]

    link_builder = lambda l: "<a href='%s' target='_blank'>%s</a>" % (l['url'], entity_text(l))

    # map starting position, length of entity placeholder to the replacement html
    entity_map = {}
    for entity_key, builder in [('links', link_builder)]:
        for entity in post.get('entities', {}).get(entity_key, []):
            entity_map[(entity['pos'], entity['len'])] = builder(entity)

    # replace strings with html
    html_pieces = []
    text_idx = 0  # our current place in the original text string
    for entity_start, entity_len in sorted(entity_map.keys()):
        if text_idx != entity_start:
            # if our current place isn't the start of an entity, bring in text until the next entity
            html_pieces.append(post.get('text', "")[text_idx:entity_start])

        # pull out the entity html
        entity_html = entity_map[(entity_start, entity_len)]
        html_pieces.append(entity_html)

        # move past the entity we just added
        text_idx = entity_start + entity_len

    # clean up any remaining text
    html_pieces.append(post.get('text', "")[text_idx:])
    html = ''.join(html_pieces)
    html = html.replace('\n', '<br>')
    # TODO: link to schema
    return '<span>%s</span>' % (html)


def get_link_for_item(feed, item):
    feed_link = feed.feed_url
    main_item_link = item.get('link')

    if not main_item_link:
        links = item.get('links', [])
        for link in links:
            href = link.get('href')
            if href:
                main_item_link = href

    # If the user hasn't turned on linked list mode
    # return the main_item_link
    if not feed.linked_list_mode:
        return main_item_link

    # if the feed is so malformed it doesn't link to it's self then
    # just return the main item link
    if not feed_link:
        return main_item_link

    parsed_feed_link = urlparse(feed_link)
    parsed_item_link = urlparse(main_item_link)

    # If the main link has the same root domain as the feed
    # This is linking back to the blog already so return
    # The main link.
    if parsed_feed_link.netloc == parsed_item_link.netloc:
        return main_item_link

    # If we are still here, we should now look through the alternate links
    links = item.get('links', [])
    for link in links:
        href = link.get('href')
        if not href:
            continue

        parsed_link = urlparse(href)
        # If we find an alternate link that matches domains
        # we make the assumption that this is the permalink back to the blog
        if parsed_link.netloc == parsed_feed_link.netloc:
            return href

    # If we are still here we now need to look through the links that are in the content
    soup = BeautifulSoup(item.get('summary', ''))
    links = soup.findAll('a')

    # No links, then we are out of look return the main_item_link
    if not links:
        return main_item_link

    # we can just ask the user to change up their content so we can find something in it
    permalink = soup.find('a', {'rel': 'permalink'})
    if permalink:
        href = permalink.get('href')
        if href:
            return href

    # one other custom method
    permalink = soup.find('a', {'rel': 'bookmark'})
    if permalink:
        href = permalink.get('href')
        if href:
            return href

    # Finally lets try a last ditch custom method
    # Right now lets just try this on the last link
    last_link = links[-1]
    # logger.info('Last link: %s', last_link)
    href = last_link.get('href')
    parsed_link = urlparse(href)
    # logger.info('Last link parsed: %s %s %s', parsed_link.netloc, parsed_feed_link.netloc, parsed_link)
    # If the domains match lets use this as the URL
    if parsed_link.netloc == parsed_feed_link.netloc:
        return href

    return main_item_link


@ndb.tasklet
def get_image_from_url(url):
    if not url:
        raise ndb.Return(None)

    # logger.info('Downloading image %s', url)
    ctx = ndb.get_context()

    if url and url.startswith('//'):
        url = 'http:' + url

    image = None

    try:
        resp = yield ctx.urlfetch(url, deadline=60)
        image = Image(image_data=resp.content)
        image.width  # Try
    except NotImageError:
        # logger.info('Image at url isnt an image: %s', url)
        pass
    except Exception, e:
        logger.exception(e)
        raise ndb.Return(None)

    raise ndb.Return(image)


MIN_IMAGE_DIMENSION = 200
MAX_IMAGE_DIMENSION = 1000


def image_fits(w, h):
    return all([w, h, w >= MIN_IMAGE_DIMENSION, w <= MAX_IMAGE_DIMENSION, h >= MIN_IMAGE_DIMENSION, h <= MAX_IMAGE_DIMENSION])


def image_dict(url, w, h):
    return {
        'thumbnail_image_url': url,
        'thumbnail_image_width': int(w),
        'thumbnail_image_height': int(h)
    }


@ndb.tasklet
def find_image_in_rss_item(item, remote_fetch=False):
    media_thumbnails = item.get('media_thumbnail') or []
    # print 'Media thumbnails %s' % (media_thumbnails)
    for thumb in media_thumbnails:
        w = int(thumb.get('width', 0))
        h = int(thumb.get('height', 0))
        if not (w and h) and remote_fetch:
            image = yield get_image_from_url(thumb['url'])
            if image:
                w = image.width
                h = image.height

        if image_fits(w, h):
            raise ndb.Return(image_dict(thumb['url'], w, h))

    raise ndb.Return(None)


@ndb.tasklet
def find_image_in_html(content, remote_fetch=False):
    soup = BeautifulSoup(content)
    for image in soup.findAll('img'):
        w = image.get('width', 0)
        h = image.get('height', 0)
        if not (w and h):
            style = parse_style_tag(image.get('style'))
            if style:
                w = style.get('width', w)
                h = style.get('height', h)

        if not(w and h):
            continue

        w, h = map(lambda x: x.replace('px', ''), (w, h))

        try:
            w = int(w)
            h = int(h)
        except:
            continue

        if image_fits(w, h):
            raise ndb.Return(image_dict(image['src'], w, h))

    # If we are still here lets grab the first image, and try and download it.
    first_image = soup.find('img')
    if first_image and remote_fetch:
        image_url = first_image.get('src')
        image = yield get_image_from_url(image_url)
        if image and image_fits(image.width, image.height):
                raise ndb.Return(image_dict(image_url, image.width, image.height))

    raise ndb.Return(None)


@ndb.tasklet
def find_image_in_meta_tags(meta_tags, remote_fetch=False):
    og = meta_tags.get('og', {})
    twitter = meta_tags.get('twitter', {})

    meta_tags_image_url = og.get('image', twitter.get('image'))
    if meta_tags_image_url and isinstance(meta_tags_image_url, dict) and meta_tags_image_url.get('url'):
        raise ndb.Return(image_dict(meta_tags_image_url['url'], meta_tags_image_url['width'],
                         meta_tags_image_url['height']))
    elif meta_tags_image_url and remote_fetch:
        image = yield get_image_from_url(meta_tags_image_url)
        try:
            if image and image_fits(image.width, image.height):
                raise ndb.Return(image_dict(meta_tags_image_url, image.width, image.height))
        except NotImageError:
            pass

    raise ndb.Return(None)


def parse_meta_data(soup):
    data = defaultdict(dict)
    props = soup.findAll('meta')

    for prop in props:
        key = prop.get('property', prop.get('name'))
        if not key:
            continue

        key = key.split(':')

        value = prop.get('content', prop.get('value'))

        if not value:
            continue

        value = value.strip()

        if value.isdigit():
            value = int(value)

        ref = data[key.pop(0)]

        for idx, part in enumerate(key):
            if not key[idx:-1]:  # no next values
                ref[part] = value
                break
            if not ref.get(part):
                ref[part] = dict()
            else:
                if isinstance(ref.get(part), basestring):
                    ref[part] = {'url': ref[part]}
            ref = ref[part]

    return data


def parse_images(soup):
    images = []
    for image in soup.findAll('img'):
        try:
            w = int(image.get('width', '').replace('px', ''))
            h = int(image.get('height', '').replace('px', ''))
        except ValueError, e:
            continue
        except Exception, e:
            logger.exception(e)
            continue

        src = image.get('src')

        if src and image_fits(w, h):
            images += [image_dict(src, w, h)]

    # We could sort images by closest to a specific aspect ratio here
    sorted(images, key=lambda x: x['thumbnail_image_width'] + x['thumbnail_image_height'])

    return images


@ndb.tasklet
def get_meta_data_for_url(url):
    ctx = ndb.get_context()

    try:
        resp = yield ctx.urlfetch(url=url, deadline=60, follow_redirects=True)
    except Exception:
        logger.exception('Failed to fetch meta data for %s' % url)
        raise ndb.Return({})

    soup = BeautifulSoup(resp.content)

    data = {
        'meta_tags': parse_meta_data(soup),
        'images_in_html': parse_images(soup),
    }
    # logger.info('Fetched meta tags: %s', data['meta_tags'])
    raise ndb.Return(data)


@ndb.tasklet
def get_short_url(entry, link, feed):
    if entry.short_url:
        raise ndb.Return(entry.short_url)

    params = {
        'login': feed.bitly_login,
        'apiKey': feed.bitly_api_key,
        'longUrl': link,
    }

    ctx = ndb.get_context()
    query_string = urllib.urlencode(params)
    resp = yield ctx.urlfetch(url='https://api-ssl.bitly.com/v3/shorten?%s' % (query_string), method='GET')
    if resp.status_code == 200:
        logger.info('url: %s Resp content: %s', 'https://api-ssl.bitly.com/v3/shorten?%s' % (query_string), resp.content)
        resp_json = json.loads(resp.content)
        if resp_json['status_code'] == 200:
            link = resp_json['data']['url']
            entry.short_url = link
            yield entry.put_async()
            raise ndb.Return(link)


@ndb.tasklet
def check_thumbnail(feed, thumbnail):
    thumbnail_hash = dict_hash(thumbnail)
    if thumbnail_hash != feed.last_image_hash:
        feed.last_image_hash = thumbnail_hash
        yield feed.put_async()
        raise ndb.Return(True)

    raise ndb.Return(False)


def prepare_title_from_item(item):
    title_detail = item.get('title_detail')
    title = item.get('title', 'No Title')

    # If the title is HTML then we need to decode it to some kind of usable text
    # Definitely need to decode any entities
    if title_detail:
        if title_detail['type'] == u'text/html':
            title = BeautifulSoup(title).text

    return title


@ndb.tasklet
def prepare_entry_from_item_local(feed, item, published, added, overflow, overflow_reason, remote_fetch):

    title = prepare_title_from_item(item)
    link = iri_to_uri(get_link_for_item(feed, item))

    # We can only store a title up to 500 chars
    title = title[0:499]
    guid = guid_for_item(item)
    if len(guid) > 500:
        logger.warn('Found a guid > 500 chars link: %s item: %s', guid, item)
        return

    if not link:
        logger.warn("Item found without link skipping item: %s", item)
        return

    if len(link) > 500:
        logger.warn('Found a link > 500 chars link: %s item: %s', link, item)
        return

    if not guid:
        logger.warn("Item found without guid skipping item: %s", item)
        return

    summary = item.get('summary', '')
    kwargs = dict(guid=guid, title=title, summary=summary, link=link,
                  published=published, overflow=overflow, overflow_reason=overflow_reason)

    if feed:
        kwargs['parent'] = feed.key

    if feed.language:
        kwargs['language'] = feed.language

    if 'tags' in item:
        kwargs['tags'] = filter(None, [x['term'] for x in item.tags])

    if 'author' in item and item.author:
        kwargs['author'] = item.author

    thumbnail = None
    try:
        if 'rss' not in feed.image_strategy_blacklist:
            thumbnail = yield find_image_in_rss_item(item, remote_fetch)

        if not thumbnail and 'content' not in feed.image_strategy_blacklist:
            thumbnail = yield find_image_in_html(summary, remote_fetch)

    except Exception, e:
        logger.info("Exception while trying to find thumbnail %s", e)
        logger.exception(e)

    # print 'thumbnail %s' % (thumbnail)

    if thumbnail:
        valid_thumbnail = yield check_thumbnail(feed, thumbnail)
        if valid_thumbnail:
            kwargs.update(thumbnail)

    kwargs['feed_item'] = item

    if added:
        kwargs['added'] = added

    raise ndb.Return(kwargs)


@ndb.tasklet
def prepare_entry_from_item_remote(feed, has_thumbnail, item_content, link, remote_fetch):
    kwargs = {}

    if remote_fetch:
        page_data = yield get_meta_data_for_url(link)
        kwargs.update(page_data)

    thumbnail = None
    try:
        if not has_thumbnail and 'meta' not in feed.image_strategy_blacklist:
            thumbnail = yield find_image_in_meta_tags(kwargs.get('meta_tags', {}), remote_fetch)
    except Exception, e:
        logger.info("Exception while trying to find thumbnail %s", e)
        logger.exception(e)

    # If we still don't have a thumbnail and we haven't blacklisted looking for images on the webpage
    # Lets take the first image on the page
    if 'html' not in feed.image_strategy_blacklist and not thumbnail and kwargs.get('images_in_html'):
        thumbnail = kwargs['images_in_html'][0]

    # print 'thumbnail %s' % (thumbnail)

    if thumbnail:
        valid_thumbnail = yield check_thumbnail(feed, thumbnail)
        if valid_thumbnail:
            kwargs.update(thumbnail)

    if feed.include_video:
        try:
            embed = yield find_video_oembed(item_content)
            if embed:
                kwargs['video_oembed'] = embed
        except Exception, e:
            logger.exception(e)
            logger.error('Finding an oebmed')

    raise ndb.Return(kwargs)


# Next steps for this function, it should probably just get a bunch of meta data at the top of the function
# And then be a bunch of functions that parse through that meta data in various manners to get the good stuff
# out of it.
@ndb.tasklet
def prepare_entry_from_item(item, feed, overflow=False, overflow_reason=None, published=False, added=None, remote_fetch=True):

    kwargs = yield prepare_entry_from_item_local(feed, item, published, added, overflow, overflow_reason, remote_fetch)
    more_kwargs = yield prepare_entry_from_item_remote(feed, bool(kwargs.get('thumbnail_image_url')), kwargs.get('summary'), kwargs.get('link'), remote_fetch)
    kwargs.update(more_kwargs)

    raise ndb.Return(kwargs)


def cross_post_annotation(link):
    return {
        "type": "net.app.core.crosspost",
        "value": {
            "canonical_url": link
        }
    }


def image_annotation_for_entry(entry):
    url, width, height = (entry.thumbnail_image_url, entry.thumbnail_image_width, entry.thumbnail_image_height)
    if getattr(entry, 'image_url', None):
        url, width, height = (entry.image_url, entry.image_width, entry.image_height)

    return {
        "type": "net.app.core.oembed",
        "value": {
            "version": "1.0",
            "type": "photo",
            "title": entry.title,
            "width": width,
            "height": height,
            "url": iri_to_uri(url),
            "thumbnail_width": entry.thumbnail_image_width,
            "thumbnail_height": entry.thumbnail_image_height,
            "thumbnail_url": iri_to_uri(entry.thumbnail_image_url),
            "embeddable_url": iri_to_uri(entry.link),
        }
    }


def metadata_annotation(entry):
    subject = ellipse_text(entry.title, 128)
    return {
        "type": 'net.app.core.broadcast.message.metadata',
        "value": {
            "subject": subject
        }
    }


def common_annotations(entry):
    annotations = []
    lang = get_language(entry.language)
    if lang:
        annotations.append({
            "type": "net.app.core.language",
            "value": {
                "language": lang,
            }
        })

    if entry.author:
        annotations.append({
            "type": "net.app.pourover.item.author",
            "value": {
                "author": entry.author,
            }
        })

    if entry.tags:
        annotations.append({
            "type": "net.app.pourover.item.tags",
            "value": {
                "tags": entry.tags,
            }
        })

    return annotations


def instagram_format_for_adn(feed, entry):
    max_chars = MAX_CHARS - len(entry.link) + 1
    post_text = ellipse_text(entry.title, max_chars)
    post_text += ' ' + entry.link
    post = {
        'text': post_text,
        'annotations': [cross_post_annotation(entry.link), image_annotation_for_entry(entry)]
    }
    return post


def format_link_for_entry(feed, entry, medium='App.net'):
    if entry.feed_item:
        link = get_link_for_item(feed, entry.feed_item)
    else:
        link = entry.link

    if link:
        link = iri_to_uri(link)
        if "utm_medium" not in link and "utm_source" not in link:
            link = append_query_string(link, params={'utm_source': 'PourOver', 'utm_medium': medium})

    return link


def broadcast_format_for_adn(feed, entry):

    #summary = clean_html(entry.summary)
    # Easy path for now, leave some space at the end for cleaning up of broken HTML
    #summary = ellipse_text(summary, 2000)
    # This will try and fix broken html
    #summary = html.tostring(html.fromstring(summary))

    link = format_link_for_entry(feed, entry, medium='App.net Broadcast')

    post = {
        'annotations': [metadata_annotation(entry), cross_post_annotation(link)]
    }

    post['annotations'] += common_annotations(entry)

    if feed.include_thumb and entry.thumbnail_image_url:
        post['annotations'].append(image_annotation_for_entry(entry))

    description = None
    if entry.meta_tags:
        og_description = entry.meta_tags.get('og', {}).get('description')
        twitter_description = entry.meta_tags.get('twitter', {}).get('description')
        description = og_description or twitter_description
        if description:
            logger.info("What are we striping html from: %s %s", description, type(description))
            description = strip_html_tags(description)

    if description:
        post['text'] = description
    else:
        post['machine_only'] = True

    return post


@ndb.tasklet
def format_for_adn(feed, entry):
    post_text = entry.title
    links = []
    summary_text = ''
    if feed.include_summary:
        summary_text = strip_html_tags(entry.summary)
        sentances = list(splitter.split(summary_text))
        sentances.reverse()
        summary_text = sentances.pop()
        while len(summary_text) <= 200:
            try:
                next_sentance = sentances.pop()
            except IndexError:
                break

            if len(summary_text + ' ' + next_sentance) <= 200:
                summary_text += ' ' + next_sentance

        summary_text = ellipse_text(summary_text, 200)

    link = format_link_for_entry(feed, entry)
    has_own_bitly_creds = feed.bitly_login and feed.bitly_api_key
    if has_own_bitly_creds or feed.format_mode == FORMAT_MODE.TITLE_THEN_LINK:
        if not has_own_bitly_creds:
            feed.bitly_login = 'mixedmedialabs'
            feed.bitly_api_key = 'R_a1311cd1785b7da2aedac9703656b0f1'

        short_url = yield get_short_url(entry, link, feed)
        if short_url:
            link = short_url

    # Starting out it should be as long as it can be
    max_chars = MAX_CHARS
    max_link_chars = 40
    ellipse_link_text = ellipse_text(link, max_link_chars)
    # If the link is to be included in the text we need to make sure we reserve enough space at the end
    if feed.format_mode == FORMAT_MODE.TITLE_THEN_LINK:
        max_chars -= len(' ' + ellipse_link_text)

    # Should be some room for a description
    if len(post_text) < (max_chars - 40) and summary_text:
        post_text = u'%s\n%s' % (post_text, summary_text)

    post_text = ellipse_text(post_text, max_chars)
    if feed.format_mode == FORMAT_MODE.TITLE_THEN_LINK:
        post_text += ' ' + ellipse_link_text

    if feed.format_mode == FORMAT_MODE.TITLE_THEN_LINK:
        links.insert(0, (link, ellipse_link_text))
    else:
        links.insert(0, (link, entry.title))

    link_entities = []
    index = 0
    for href, link_text in links:
        # logger.info('Link info: %s %s %s', post_text, link_text, index)
        text_index = post_text.find(link_text, index)
        if text_index > -1:
            link_entities.append({
                'url': href,
                'text': link_text,
                'pos': text_index,
                'len': len(link_text),
            })
            index = text_index

    post = {
        'text': post_text,
        'annotations': [cross_post_annotation(link)]
    }

    if link_entities:
        post['entities'] = {
            'links': link_entities,
        }

    # logger.info('Info %s, %s', include_thumb, self.thumbnail_image_url)
    if feed.include_thumb and entry.thumbnail_image_url:
        post['annotations'].append(image_annotation_for_entry(entry))

    if feed.include_video and entry.video_oembed:
        oembed = entry.video_oembed
        oembed['embeddable_url'] = entry.link
        post['annotations'].append({
            "type": "net.app.core.oembed",
            "value": oembed
        })

    post['annotations'] += common_annotations(entry)

    raise ndb.Return(post)

# Copyright 2004-2022 Davide Alberani <da@erlug.linux.it>
#                2008 H. Turgut Uyar <uyar@tekir.org>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

"""
This package provides the IMDbHTTPAccessSystem class used to access IMDb's data
through the web interface.

The :func:`imdb.IMDb` function will return an instance of this class when
called with the ``accessSystem`` argument is set to "http" or "web"
or "html" (this is the default).
"""

import html as html_lib
import json
import re
import ssl
import warnings
from codecs import lookup
from difflib import SequenceMatcher
from urllib.parse import quote, quote_plus
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from imdb import IMDbBase
from imdb._exceptions import IMDbDataAccessError, IMDbParserError
from imdb.Movie import Movie
from imdb.parser.http.logging import logger
from imdb.utils import analyze_company_name, analyze_name, analyze_title
from .utils import build_movie
from . import (
    companyParser,
    listParser,
    movieParser,
    personParser,
    searchCompanyParser,
    searchKeywordParser,
    searchMovieAdvancedParser,
    searchMovieParser,
    searchPersonParser,
    showtimesParser,
    topBottomParser,
)

# Logger for miscellaneous functions.
_aux_logger = logger.getChild('aux')


class _ModuleProxy:
    """A proxy to instantiate and access parsers."""
    def __init__(self, module, defaultKeys=None):
        """Initialize a proxy for the given module; defaultKeys, if set,
        muste be a dictionary of values to set for instanced objects."""
        if defaultKeys is None:
            defaultKeys = {}
        self._defaultKeys = defaultKeys
        self._module = module

    def __getattr__(self, name):
        """Called only when no look-up is found."""
        _sm = self._module
        # Read the _OBJECTS dictionary to build the asked parser.
        if name in _sm._OBJECTS:
            _entry = _sm._OBJECTS[name]
            # Initialize the parser.
            kwds = {}
            parserClass = _entry[0][0]
            obj = parserClass(**kwds)
            attrsToSet = self._defaultKeys.copy()
            attrsToSet.update(_entry[1] or {})
            # Set attribute to the object.
            for key in attrsToSet:
                setattr(obj, key, attrsToSet[key])
            setattr(self, name, obj)
            return obj
        return getattr(_sm, name)


class _FakeURLOpener:
    """Fake URLOpener object, used to return empty strings instead of
    errors.
    """
    def __init__(self, url, headers):
        self.url = url
        self.headers = headers

    def read(self, *args, **kwds):
        return ''

    def close(self, *args, **kwds):
        pass

    def info(self, *args, **kwds):
        return self.headers


class IMDbHTTPSHandler(HTTPSHandler, object):
    """HTTPSHandler that ignores the SSL certificate."""
    def __init__(self, logger=None, *args, **kwds):
        self._logger = logger
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        super().__init__(context=context)

    def http_error_default(self, url, fp, errcode, errmsg, headers):
        if errcode == 404:
            if self._logger:
                self._logger.warn('404 code returned for %s: %s (headers: %s)',
                                  url, errmsg, headers)
            return _FakeURLOpener(url, headers)
        raise IMDbDataAccessError(
            {'url': 'http:%s' % url,
             'errcode': errcode,
             'errmsg': errmsg,
             'headers': headers,
             'error type': 'http_error_default',
             'proxy': self.get_proxy()}
        )

    def open_unknown(self, fullurl, data=None):
        raise IMDbDataAccessError(
            {'fullurl': fullurl,
             'data': str(data),
             'error type': 'open_unknown',
             'proxy': self.get_proxy()}
        )

    def open_unknown_proxy(self, proxy, fullurl, data=None):
        raise IMDbDataAccessError(
            {'proxy': str(proxy),
             'fullurl': fullurl,
             'error type': 'open_unknown_proxy',
             'data': str(data)}
        )


class IMDbHTTPRedirectHandler(HTTPRedirectHandler):
    """Custom handler to support redirect 308."""
    def http_error_308(self, req, fp, code, msg, headers):
        # force handling of redirect 308
        req.code = 302
        code = 302
        return super().http_error_302(req, fp, code, msg, headers)


class IMDbURLopener:
    """Fetch web pages and handle errors."""
    _logger = logger.getChild('urlopener')

    def __init__(self, *args, **kwargs):
        self._last_url = ''
        self._last_waf_action = None
        self.https_handler = IMDbHTTPSHandler(logger=self._logger)
        self.redirect_handler = IMDbHTTPRedirectHandler()
        self.proxies = {}
        self.addheaders = []
        for header in ('User-Agent', 'User-agent', 'user-agent'):
            self.del_header(header)
        self.set_header('User-Agent',
                        'Mozilla/5.0 (X11; CrOS armv6l 13597.84.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36 Edg/107.0.1418.56')  # noqa: E501
        lang = kwargs.get('languages', 'en-us,en;q=0.5')
        self.set_header('Accept-Language', lang)

    def get_proxy(self):
        """Return the used proxy, or an empty string."""
        return self.proxies.get('http', '')

    def set_proxy(self, proxy):
        """Set the proxy."""
        if not proxy:
            if 'http' in self.proxies:
                del self.proxies['http']
        else:
            if not proxy.lower().startswith('http://'):
                proxy = 'http://%s' % proxy
            self.proxies['http'] = proxy

    def set_header(self, header, value, _overwrite=True):
        """Set a default header."""
        if _overwrite:
            self.del_header(header)
        self.addheaders.append((header, value))

    def get_header(self, header):
        """Return the first value of a header, or None
        if not present."""
        for index in range(len(self.addheaders)):
            if self.addheaders[index][0] == header:
                return self.addheaders[index][1]
        return None

    def del_header(self, header):
        """Remove a default header."""
        for index in range(len(self.addheaders)):
            if self.addheaders[index][0] == header:
                del self.addheaders[index]
                break

    def retrieve_unicode(self, url, size=-1, timeout=None):
        """Retrieves the given URL, and returns a unicode string,
        trying to guess the encoding of the data (assuming utf8
        by default)"""
        encode = None
        try:
            if size != -1:
                self.set_header('Range', 'bytes=0-%d' % size)
            handlers = []
            if 'http' in self.proxies:
                proxy_handler = ProxyHandler({
                    'http': self.proxies['http'],
                    'https': self.proxies['http']
                })
                handlers.append(proxy_handler)
            handlers.append(self.redirect_handler)
            handlers.append(self.https_handler)
            uopener = build_opener(*handlers)
            uopener.addheaders = list(self.addheaders)
            response = uopener.open(url, timeout=timeout)
            content = response.read()
            self._last_url = response.url
            self._last_waf_action = response.headers.get('x-amzn-waf-action')
            # Maybe the server is so nice to tell us the charset...
            server_encode = response.headers.get_content_charset(None)
            # Otherwise, look at the content-type HTML meta tag.
            if server_encode is None and content:
                begin_h = content.find(b'text/html; charset=')
                if begin_h != -1:
                    end_h = content[19 + begin_h:].find('"')
                    if end_h != -1:
                        server_encode = content[19 + begin_h:19 + begin_h + end_h]
            if server_encode:
                try:
                    if lookup(server_encode):
                        encode = server_encode
                except (LookupError, ValueError, TypeError):
                    pass
            if size != -1:
                self.del_header('Range')
            response.close()
        except IOError as e:
            if size != -1:
                # Ensure that the Range header is removed.
                self.del_header('Range')
            raise IMDbDataAccessError(
                {'errcode': e.errno,
                 'errmsg': str(e.strerror),
                 'url': url,
                 'proxy': self.get_proxy(),
                 'exception type': 'IOError',
                 'original exception': e}
            )
        if encode is None:
            encode = 'utf8'
            # The detection of the encoding is error prone...
            self._logger.warn('Unable to detect the encoding of the retrieved page [%s];'
                              ' falling back to default utf8.', encode)
        if isinstance(content, str):
            return content
        return str(content, encode, 'replace')


class IMDbHTTPAccessSystem(IMDbBase):
    """The class used to access IMDb's data through the web."""

    accessSystem = 'http'
    _http_logger = logger

    def __init__(self, adultSearch=True, proxy=-1, cookie_id=-1,
                 timeout=30, cookie_uu=None, *arguments, **keywords):
        """Initialize the access system."""
        IMDbBase.__init__(self, *arguments, **keywords)
        self.urlOpener = IMDbURLopener(*arguments, **keywords)
        self._getRefs = True
        self._mdparse = False
        self.timeout = timeout
        if proxy != -1:
            self.set_proxy(proxy)
        _def = {'_modFunct': self._defModFunct, '_as': self.accessSystem}

        # Proxy objects.
        self.smProxy = _ModuleProxy(searchMovieParser, defaultKeys=_def)
        self.smaProxy = _ModuleProxy(searchMovieAdvancedParser, defaultKeys=_def)
        self.spProxy = _ModuleProxy(searchPersonParser, defaultKeys=_def)
        self.scompProxy = _ModuleProxy(searchCompanyParser, defaultKeys=_def)
        self.skProxy = _ModuleProxy(searchKeywordParser, defaultKeys=_def)
        self.mProxy = _ModuleProxy(movieParser, defaultKeys=_def)
        self.pProxy = _ModuleProxy(personParser, defaultKeys=_def)
        self.compProxy = _ModuleProxy(companyParser, defaultKeys=_def)
        self.topBottomProxy = _ModuleProxy(topBottomParser, defaultKeys=_def)
        self.listProxy = _ModuleProxy(listParser, defaultKeys=_def)
        self.stimesProxy = _ModuleProxy(showtimesParser, defaultKeys=_def)

    def _normalize_movieID(self, movieID):
        """Normalize the given movieID."""
        try:
            return '%07d' % int(movieID)
        except ValueError as e:
            raise IMDbParserError('invalid movieID "%s": %s' % (movieID, e))

    def _normalize_personID(self, personID):
        """Normalize the given personID."""
        try:
            return '%07d' % int(personID)
        except ValueError as e:
            raise IMDbParserError('invalid personID "%s": %s' % (personID, e))

    def _normalize_companyID(self, companyID):
        """Normalize the given companyID."""
        try:
            return '%07d' % int(companyID)
        except ValueError as e:
            raise IMDbParserError('invalid companyID "%s": %s' % (companyID, e))

    def get_imdbMovieID(self, movieID):
        """Translate a movieID in an imdbID; in this implementation
        the movieID _is_ the imdbID.
        """
        return movieID

    def get_imdbPersonID(self, personID):
        """Translate a personID in an imdbID; in this implementation
        the personID _is_ the imdbID.
        """
        return personID

    def get_imdbCompanyID(self, companyID):
        """Translate a companyID in an imdbID; in this implementation
        the companyID _is_ the imdbID.
        """
        return companyID

    def get_proxy(self):
        """Return the used proxy or an empty string."""
        return self.urlOpener.get_proxy()

    def set_proxy(self, proxy):
        """Set the web proxy to use.

        It should be a string like 'http://localhost:8080/'; if the
        string is empty, no proxy will be used.
        If set, the value of the environment variable HTTP_PROXY is
        automatically used.
        """
        self.urlOpener.set_proxy(proxy)

    def set_cookies(self, cookie_id, cookie_uu):
        """Set a cookie to access an IMDb's account."""
        warnings.warn("set_cookies has been deprecated")

    def del_cookies(self):
        """Remove the used cookie."""
        warnings.warn("del_cookies has been deprecated")

    def do_adult_search(self, doAdult,
                        cookie_id=None, cookie_uu=None):
        """If doAdult is true, 'adult' movies are included in the
        search results; cookie_id and cookie_uu are optional
        parameters to select a specific account (see your cookie
        or cookies.txt file."""
        return

    def _retrieve(self, url, size=-1, _noCookies=False, _allowWaf=False):
        """Retrieve the given URL."""
        self._http_logger.debug('fetching url %s (size: %d)', url, size)
        ret = self.urlOpener.retrieve_unicode(url, size=size, timeout=self.timeout)
        waf_action = self.urlOpener._last_waf_action
        if waf_action and not _allowWaf:
            raise IMDbDataAccessError(
                {'errcode': 202,
                 'errmsg': 'IMDb returned an AWS WAF %s response' % waf_action,
                 'url': url,
                 'proxy': self.get_proxy(),
                 'exception type': 'IMDbWAFChallenge'}
            )
        return ret

    def _suggestion_url(self, query):
        """Return the IMDb suggestion endpoint URL for a search string."""
        query = (query or '').strip()
        first = next((c.lower() for c in query if c.isalnum()), '_')
        return (
            'https://v3.sg.media-imdb.com/suggestion/%s/%s.json'
            '?includeVideos=0'
        ) % (first, quote(query, safe=''))

    def _search_suggestion(self, query, kind, results):
        """Fallback search using IMDb's public suggestion JSON endpoint."""
        try:
            content = self.urlOpener.retrieve_unicode(
                self._suggestion_url(query), timeout=self.timeout
            )
            payload = json.loads(content or '{}')
        except Exception:
            self._http_logger.warn('unable to use IMDb suggestion search for %s',
                                   query, exc_info=True)
            return []

        parsed = []
        for item in payload.get('d') or []:
            imdb_id = item.get('id') or ''
            label = item.get('l') or ''
            if not (imdb_id and label):
                continue
            if kind == 'tt' and not imdb_id.startswith('tt'):
                continue
            if kind == 'nm' and not imdb_id.startswith('nm'):
                continue
            if kind == 'co' and not imdb_id.startswith('co'):
                continue

            data = {}
            img = item.get('i') or {}
            image_url = img.get('imageUrl')
            if kind == 'tt':
                data['title'] = label
                if item.get('y'):
                    data['year'] = item['y']
                qid = (item.get('qid') or item.get('q') or '').strip()
                kind_map = {
                    'movie': 'movie',
                    'tvmovie': 'tv movie',
                    'tv movie': 'tv movie',
                    'tvseries': 'tv series',
                    'tv series': 'tv series',
                    'tvminiseries': 'tv mini series',
                    'tv mini series': 'tv mini series',
                    'tvepisode': 'episode',
                    'tv episode': 'episode',
                    'video': 'video movie',
                    'videogame': 'video game',
                    'video game': 'video game',
                    'short': 'short',
                }
                qid_key = qid.replace('-', '').replace('_', '').lower()
                if qid_key in kind_map:
                    data['kind'] = kind_map[qid_key]
                if image_url:
                    data['cover url'] = image_url
                    data['full-size cover url'] = image_url
                parsed.append((imdb_id[2:], data))
            elif kind == 'nm':
                data = analyze_name(label, canonical=1)
                if image_url:
                    data['headshot'] = image_url
                parsed.append((imdb_id[2:], data))
            elif kind == 'co':
                parsed.append((imdb_id[2:], analyze_company_name(label)))

            if len(parsed) >= results:
                break
        return parsed

    def _search_graphql(self, query_text, kind, results, prefer_original_title=False):
        """Fallback search using IMDb's GraphQL mainSearch endpoint."""
        search_types = {'tt': 'TITLE', 'nm': 'NAME'}.get(kind)
        if not search_types:
            return []
        query = """
        query {
          mainSearch(
            first: %d
            options: {
              searchTerm: %s
              isExactMatch: false
              type: [%s]
              titleSearchOptions: { type: [] }
            }
          ) {
            edges {
              node {
                entity {
                  ... on Title {
                    __typename
                    id
                    titleText { text }
                    originalTitleText { text }
                    releaseYear { year }
                    releaseDate { year month day }
                    primaryImage { url }
                    titleType { id text }
                    ratingsSummary { aggregateRating }
                    runtime { seconds }
                  }
                  ... on Name {
                    __typename
                    id
                    nameText { text }
                    primaryImage { url }
                    knownForV2 {
                      credits {
                        title { titleText { text } releaseYear { year } }
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """ % (max(results, 1), json.dumps(query_text), search_types)
        payload = self._post_graphql(query, query_text)
        edges = (((payload.get('data') or {}).get('mainSearch') or {}).get('edges') or [])
        parsed = []
        kind_map = {
            'movie': 'movie',
            'tvMovie': 'tv movie',
            'tvSeries': 'tv series',
            'tvMiniSeries': 'tv mini series',
            'tvEpisode': 'episode',
            'video': 'video movie',
            'videoGame': 'video game',
            'short': 'short',
        }
        for edge in edges:
            entity = ((edge.get('node') or {}).get('entity') or {})
            imdb_id = entity.get('id') or ''
            image_url = (entity.get('primaryImage') or {}).get('url')
            if kind == 'tt':
                if not imdb_id.startswith('tt'):
                    continue
                title_text = (entity.get('titleText') or {}).get('text')
                original_title = (entity.get('originalTitleText') or {}).get('text')
                title = title_text
                if prefer_original_title:
                    title = original_title or title_text
                if not title:
                    continue
                data = {'title': title}
                if title_text and title_text != title:
                    data['localized title'] = title_text
                if original_title and original_title != title:
                    data['original title'] = original_title
                release_year = entity.get('releaseYear') or {}
                release_date = entity.get('releaseDate') or {}
                if release_year.get('year'):
                    data['year'] = release_year['year']
                elif release_date.get('year'):
                    data['year'] = release_date['year']
                type_id = (entity.get('titleType') or {}).get('id')
                if type_id in kind_map:
                    data['kind'] = kind_map[type_id]
                rating = (entity.get('ratingsSummary') or {}).get('aggregateRating')
                if rating is not None:
                    data['rating'] = rating
                runtime = entity.get('runtime') or {}
                if runtime.get('seconds'):
                    data['runtimes'] = [str(int(runtime['seconds'] / 60))]
                if image_url:
                    data['cover url'] = image_url
                    data['full-size cover url'] = image_url
                parsed.append((imdb_id[2:], data))
            elif kind == 'nm':
                if not imdb_id.startswith('nm'):
                    continue
                name = (entity.get('nameText') or {}).get('text')
                if not name:
                    continue
                data = analyze_name(name, canonical=1)
                if image_url:
                    data['headshot'] = image_url
                parsed.append((imdb_id[2:], data))
            if len(parsed) >= results:
                break
        return parsed

    def _search_result_score(self, query, movieID, data, position):
        """Score merged search results so closest title matches appear first."""
        query_l = (query or '').lower().strip()
        query_clean = re.sub(r'[^a-z0-9]', '', query_l)
        title = ((data or {}).get('title') or '').strip()
        title_l = title.lower()
        title_clean = re.sub(r'[^a-z0-9]', '', title_l)

        score = 0
        if title_clean and title_clean == query_clean:
            score += 10000
        elif title_clean and title_clean.startswith(query_clean):
            score += 7000
        elif query_clean and query_clean in title_clean:
            score += 4000
        elif title_clean and title_clean in query_clean:
            score += 3000
        elif query_clean and title_clean:
            ratio = SequenceMatcher(None, query_clean, title_clean).ratio()
            if ratio >= 0.70:
                score += int(ratio * 3500)

        query_year = re.search(r'(?:19|20)\d{2}', query_l)
        query_year = query_year.group(0) if query_year else None
        if query_year and str((data or {}).get('year') or '') == query_year:
            score += 8000

        if (data or {}).get('localized title'):
            score += 50

        return score - position

    def _merge_search_results(self, primary, extra, results, query=None):
        """Merge search result tuples while preserving same-id alternate titles."""
        merged = []
        seen = set()
        max_results = max(results, 1) * 2
        for movieID, data in (primary or []) + (extra or []):
            title = (data or {}).get('title') or ''
            key = (movieID, title.lower())
            if key in seen:
                continue
            seen.add(key)
            merged.append((movieID, data))
            if len(merged) >= max_results:
                break
        if query:
            merged = [
                item for _, item in sorted(
                    (
                        (self._search_result_score(query, movieID, data, pos),
                         (movieID, data))
                        for pos, (movieID, data) in enumerate(merged)
                    ),
                    key=lambda item: item[0],
                    reverse=True,
                )
            ]
        return merged

    def _search_suggestion_with_originals(self, query, kind, results):
        """IMDb suggestion JSON plus IMDBKit-style GraphQL original-title matches."""
        suggestion = self._search_suggestion(query, kind, results)
        if kind != 'tt':
            return suggestion
        graphql = self._search_graphql(
            query, kind, results, prefer_original_title=True
        )
        return self._merge_search_results(suggestion, graphql, results, query=query)

    def _get_movie_graphql(self, movieID):
        """Fetch core title data from IMDb's GraphQL endpoint."""
        imdb_id = 'tt%s' % movieID
        query = """
        query {
          title(id: "%s") {
            titleText { text }
            originalTitleText { text }
            titleType { id text }
            releaseYear { year endYear }
            releaseDate { day month year }
            ratingsSummary { aggregateRating voteCount }
            genres { genres { text } }
            runtime { seconds }
            plot { plotText { plainText } }
            primaryImage { url }
            spokenLanguages { spokenLanguages { text id } }
            countriesOfOrigin { countries { text id } }
            certificate { rating }
          }
        }
        """ % imdb_id
        payload = json.dumps({
            'query': query,
        }).encode('utf8')
        req = Request(
            'https://graphql.imdb.com/',
            data=payload,
            headers={
                'Content-Type': 'application/json',
                'User-Agent': self.urlOpener.get_header('User-Agent') or 'Mozilla/5.0',
                'Accept-Language': self.urlOpener.get_header('Accept-Language') or 'en-us,en;q=0.5',
            },
        )
        try:
            response = build_opener().open(req, timeout=self.timeout)
            content = response.read()
            response.close()
            title = json.loads(content.decode('utf8')).get('data', {}).get('title')
        except Exception:
            self._http_logger.warn('unable to use IMDb GraphQL for %s',
                                   imdb_id, exc_info=True)
            return {}
        if not title:
            return {}

        data = {'imdbID': movieID}
        title_text = (title.get('titleText') or {}).get('text')
        if title_text:
            data['title'] = title_text
        original_title = (title.get('originalTitleText') or {}).get('text')
        if original_title:
            data['original title'] = original_title

        type_id = (title.get('titleType') or {}).get('id')
        kind_map = {
            'movie': 'movie',
            'tvMovie': 'tv movie',
            'tvSeries': 'tv series',
            'tvMiniSeries': 'tv mini series',
            'tvEpisode': 'episode',
            'video': 'video movie',
            'videoGame': 'video game',
            'short': 'short',
        }
        if type_id in kind_map:
            data['kind'] = kind_map[type_id]

        release_year = title.get('releaseYear') or {}
        if release_year.get('year'):
            data['year'] = release_year['year']
            if release_year.get('endYear'):
                data['year'] = '%s-%s' % (data['year'], release_year['endYear'])

        release_date = title.get('releaseDate') or {}
        if release_date.get('year'):
            parts = [
                str(release_date.get('day') or '').strip(),
                str(release_date.get('month') or '').strip(),
                str(release_date.get('year') or '').strip(),
            ]
            data['original air date'] = '-'.join([p for p in parts if p])

        ratings = title.get('ratingsSummary') or {}
        if ratings.get('aggregateRating') is not None:
            data['rating'] = ratings['aggregateRating']
        if ratings.get('voteCount') is not None:
            data['votes'] = ratings['voteCount']

        genres = [
            g.get('text') for g in
            (title.get('genres') or {}).get('genres') or []
            if g.get('text')
        ]
        if genres:
            data['genres'] = genres

        runtime = title.get('runtime') or {}
        if runtime.get('seconds'):
            data['runtimes'] = [str(int(runtime['seconds'] / 60))]

        plot = ((title.get('plot') or {}).get('plotText') or {}).get('plainText')
        if plot:
            data['plot'] = [plot]
            data['plot outline'] = plot

        image_url = (title.get('primaryImage') or {}).get('url')
        if image_url:
            data['cover url'] = image_url
            data['full-size cover url'] = image_url

        languages = [
            lang.get('text') for lang in
            (title.get('spokenLanguages') or {}).get('spokenLanguages') or []
            if lang.get('text')
        ]
        if languages:
            data['languages'] = languages

        countries = [
            country.get('text') for country in
            (title.get('countriesOfOrigin') or {}).get('countries') or []
            if country.get('text')
        ]
        if countries:
            data['countries'] = countries

        certificate = (title.get('certificate') or {}).get('rating')
        if certificate:
            data['certificates'] = [certificate]
        return data

    def _post_graphql(self, query, query_term):
        """Run a GraphQL query against IMDb and return the decoded payload."""
        payload = json.dumps({'query': query}).encode('utf8')
        req = Request(
            'https://api.graphql.imdb.com/',
            data=payload,
            headers={
                'Content-Type': 'application/json',
                'User-Agent': self.urlOpener.get_header('User-Agent') or 'Mozilla/5.0',
                'Accept-Language': self.urlOpener.get_header('Accept-Language') or 'en-us,en;q=0.5',
                'x-imdb-user-country': 'US',
            },
        )
        try:
            response = build_opener().open(req, timeout=self.timeout)
            content = response.read()
            response.close()
            data = json.loads(content.decode('utf8'))
        except Exception:
            self._http_logger.warn('unable to use IMDb GraphQL for %s',
                                   query_term, exc_info=True)
            return {}
        if data.get('errors'):
            self._http_logger.warn('IMDb GraphQL returned errors for %s: %s',
                                   query_term, data.get('errors'))
            return {}
        return data

    def _plain_text(self, value):
        """Convert IMDb plaidHtml/plain text fragments to a compact string."""
        if not value:
            return ''
        value = re.sub(r'<br\s*/?>', '\n', value)
        value = re.sub(r'<[^>]+>', '', value)
        value = html_lib.unescape(value)
        return re.sub(r'[ \t\r\f\v]+', ' ', value).strip()

    def _get_movie_graphql_extended(self, movieID):
        """Fetch GraphQL-only title extras used as HTML fallbacks."""
        imdb_id = 'tt%s' % movieID
        query = """
        query {
          title(id: "%s") {
            id
            interests(first: 20) {
              edges { node { primaryText { text } } }
            }
            akas(first: 200) {
              edges {
                node {
                  country { name: text code: id }
                  language { name: text code: id }
                  title: text
                }
              }
            }
            trivia(first: 50) {
              edges {
                node {
                  id
                  displayableArticle { body { plaidHtml } }
                  interestScore { usersVoted usersInterested }
                }
              }
            }
            reviews(first: 50) {
              edges {
                node {
                  id
                  spoiler
                  author { nickName }
                  summary { originalText }
                  text { originalText { plaidHtml } }
                  authorRating
                  submissionDate
                  helpfulness { upVotes downVotes }
                }
              }
            }
            parentsGuide {
              categories {
                category { id text }
                guideItems(first: 10) {
                  edges { node { isSpoiler text { plaidHtml } } }
                }
                severity { id votedFor }
                severityBreakdown { votedFor voteType }
              }
            }
          }
        }
        """ % imdb_id
        data = self._post_graphql(query, imdb_id)
        return data.get('data', {}).get('title') or {}

    def _graphql_akas_data(self, movieID):
        raw = self._get_movie_graphql_extended(movieID)
        akas = []
        raw_akas = []
        for edge in (raw.get('akas') or {}).get('edges') or []:
            node = edge.get('node') or {}
            title = node.get('title')
            if not title:
                continue
            country = (node.get('country') or {}).get('name')
            language = (node.get('language') or {}).get('name')
            if country:
                akas.append('%s (%s)' % (title, country))
            else:
                akas.append(title)
            raw_akas.append({
                'title': title,
                'country': country,
                'language': language,
            })
        data = {}
        if akas:
            data['akas'] = data['akas from release info'] = akas
            data['raw akas'] = raw_akas
        return {'data': data, 'info sets': ('release dates', 'akas')}

    def _graphql_trivia_data(self, movieID):
        raw = self._get_movie_graphql_extended(movieID)
        trivia = []
        for edge in (raw.get('trivia') or {}).get('edges') or []:
            body = (((edge.get('node') or {}).get('displayableArticle') or {})
                    .get('body') or {}).get('plaidHtml')
            text = self._plain_text(body)
            if text:
                trivia.append(text)
        return {'data': {'trivia': trivia} if trivia else {}, 'info sets': ('trivia',)}

    def _graphql_reviews_data(self, movieID):
        raw = self._get_movie_graphql_extended(movieID)
        reviews = []
        for edge in (raw.get('reviews') or {}).get('edges') or []:
            node = edge.get('node') or {}
            content = self._plain_text(
                ((node.get('text') or {}).get('originalText') or {}).get('plaidHtml')
            )
            if not content:
                continue
            helpfulness = node.get('helpfulness') or {}
            reviews.append({
                'content': content,
                'title': ((node.get('summary') or {}).get('originalText') or '').strip(),
                'author': None,
                'author_name': ((node.get('author') or {}).get('nickName') or '').strip(),
                'date': node.get('submissionDate'),
                'rating': node.get('authorRating'),
                'helpful': helpfulness.get('upVotes') or 0,
                'not_helpful': helpfulness.get('downVotes') or 0,
            })
        return {'data': {'reviews': reviews} if reviews else {}, 'info sets': ('reviews',)}

    def _graphql_parental_guide_data(self, movieID):
        raw = self._get_movie_graphql_extended(movieID)
        data = {}
        for category in ((raw.get('parentsGuide') or {}).get('categories') or []):
            name = ((category.get('category') or {}).get('text') or '').lower()
            if not name:
                continue
            key = 'advisory %s' % name.replace('&', 'and')
            items = []
            for edge in (category.get('guideItems') or {}).get('edges') or []:
                text = self._plain_text(((edge.get('node') or {}).get('text') or {}).get('plaidHtml'))
                if text:
                    items.append(text)
            if items:
                data[key] = items
            severity = category.get('severity') or {}
            if severity.get('id'):
                data.setdefault('advisory votes', {})[name] = {
                    'status': severity.get('id'),
                    'votes': severity.get('votedFor'),
                }
        return {'data': data, 'info sets': ('parents guide',)}

    def _graphql_person_filmography_data(self, personID):
        imdb_id = 'nm%s' % personID
        query = """
        query {
          name(id: "%s") {
            nameText { text }
            credits(first: 250) {
              edges {
                node {
                  category { id }
                  title {
                    id
                    ratingsSummary { aggregateRating }
                    primaryImage { url }
                    originalTitleText { text }
                    titleText { text }
                    titleType { id }
                    releaseYear { year }
                  }
                }
              }
            }
          }
        }
        """ % imdb_id
        payload = self._post_graphql(query, imdb_id)
        raw = payload.get('data', {}).get('name') or {}
        filmo = {}
        for edge in (raw.get('credits') or {}).get('edges') or []:
            node = edge.get('node') or {}
            title = node.get('title') or {}
            title_id = title.get('id') or ''
            if not title_id.startswith('tt'):
                continue
            role = (node.get('category') or {}).get('id') or 'unknown'
            title_text = ((title.get('titleText') or {}).get('text') or
                          (title.get('originalTitleText') or {}).get('text') or '')
            if not title_text:
                continue
            year = (title.get('releaseYear') or {}).get('year')
            movie = build_movie(title_text, movieID=title_id[2:], year=str(year) if year else None)
            type_id = (title.get('titleType') or {}).get('id')
            kind_map = {
                'movie': 'movie',
                'tvMovie': 'tv movie',
                'tvSeries': 'tv series',
                'tvMiniSeries': 'tv mini series',
                'tvEpisode': 'episode',
                'video': 'video movie',
                'videoGame': 'video game',
                'short': 'short',
            }
            if type_id in kind_map:
                movie['kind'] = kind_map[type_id]
            rating = (title.get('ratingsSummary') or {}).get('aggregateRating')
            if rating is not None:
                movie['rating'] = rating
            image = (title.get('primaryImage') or {}).get('url')
            if image:
                movie['cover url'] = image
            filmo.setdefault(role, []).append(movie)
        return {'data': {'filmography': filmo} if filmo else {},
                'info sets': ('filmography',)}

    def _get_search_content(self, kind, ton, results):
        """Retrieve the web page for a given search.
        kind can be 'tt' (for titles), 'nm' (for names),
        or 'co' (for companies).
        ton is the title or the name to search.
        results is the maximum number of results to be retrieved."""
        params = 'q=%s&s=%s' % (quote_plus(ton, safe=''), kind)
        if kind == 'ep':
            params = params.replace('s=ep&', 's=tt&ttype=ep&', 1)
        cont = self._retrieve(self.urls['find'] % params, _allowWaf=True)
        if self.urlOpener._last_waf_action:
            return ''
        # print 'URL:', imdbURL_find % params
        if cont.find('Your search returned more than') == -1 or \
                cont.find("displayed the exact matches") == -1:
            return cont
        # The retrieved page contains no results, because too many
        # titles or names contain the string we're looking for.
        params = 'q=%s&ls=%s&lm=0' % (quote_plus(ton, safe=''), kind)
        size = 131072 + results * 512
        cont = self._retrieve(self.urls['find'] % params, size=size, _allowWaf=True)
        if self.urlOpener._last_waf_action:
            return ''
        return cont

    def _search_movie(self, title, results):
        try:
            cont = self._get_search_content('tt', title, results)
        except IMDbDataAccessError:
            return self._search_suggestion_with_originals(title, 'tt', results)
        data = self.smProxy.search_movie_parser.parse(cont, results=results)['data']
        if data:
            return data
        return self._search_suggestion_with_originals(title, 'tt', results)

    def _get_list_content(self, list_, page):
        """Retrieve a list by it's id"""
        if list_.startswith('ls'):
            imdbUrl = self.urls['movie_list'] + list_ + '?page=' + str(page)
        else:
            warnings.warn("list type not recognized make sure it starts with 'ls'")
            return
        return self._retrieve(url=imdbUrl)

    def _get_movie_list(self, list_, results):
        page = 1
        result_list = []
        while True:
            cont = self._get_list_content(list_, page=page)
            result_part = self.listProxy.list_parser.parse(cont, results=results)['data']
            if result_part:
                page += 1
                result_list.extend(result_part)
            else:
                break
        return result_list

    def _get_search_movie_advanced_content(self, title=None, adult=None, results=None,
                                           sort=None, sort_dir=None, title_types=None,
                                           after=None):
        """Retrieve the web page for a given search.
        results is the maximum number of results to be retrieved.
        after is the cursor for pagination."""
        criteria = {}
        if title is not None:
            criteria['title'] = quote_plus(title, safe='')
        if adult:
            criteria['adult'] = 'include'
        if results is not None:
            criteria['count'] = str(results)
        if sort is not None:
            criteria['sort'] = sort
            if sort_dir is not None:
                criteria['sort'] = sort + ',' + sort_dir
        if title_types is not None:
            criteria['title_type'] = ','.join(title_types)
        if after is not None:
            criteria['after'] = after
        params = '&'.join(['%s=%s' % (k, v) for k, v in criteria.items()])
        return self._retrieve(self.urls['search_movie_advanced'] % params)

    def _search_movie_advanced(self, title=None, adult=None, results=None, sort=None, sort_dir=None,
                               title_types=None):
        """Search for movies with advanced options.
        Supports multi-page fetching to retrieve more than 50 results."""
        if results is None:
            results = 20  # Default to 20 results like the original
        # IMDB returns up to 50 results per page
        page_size = min(results, 50)
        all_results = []
        cursor = None
        max_pages = 10  # Safety limit to prevent infinite loops

        for _ in range(max_pages):
            cont = self._get_search_movie_advanced_content(
                title=title, adult=adult, results=page_size,
                sort=sort, sort_dir=sort_dir, title_types=title_types,
                after=cursor
            )
            parsed = self.smaProxy.search_movie_advanced_parser.parse(cont)
            page_data = parsed.get('data', [])
            all_results.extend(page_data)

            # Check if we have enough results or no more pages
            if len(all_results) >= results:
                break
            cursor = parsed.get('next_cursor')
            if not cursor:
                break

        # Trim to requested number of results
        return all_results[:results]

    def _get_top_movies_or_tv_by_genres(self, genres, filter_content):
        cont = self._retrieve(self.urls['search_movie_advanced'] % 'genres=' + genres + filter_content)
        return self.smaProxy.search_movie_advanced_parser.parse(cont)['data']

    def _search_episode(self, title, results):
        t_dict = analyze_title(title)
        if t_dict['kind'] == 'episode':
            title = t_dict['title']
        cont = self._get_search_content('ep', title, results)
        data = self.smProxy.search_movie_parser.parse(cont, results=results)['data']
        if data:
            return data
        return self._search_suggestion_with_originals(title, 'tt', results)

    def get_movie_main(self, movieID):
        try:
            cont = self._retrieve(self.urls['movie_main'] % movieID + 'reference',
                                  _allowWaf=True)
        except IMDbDataAccessError:
            cont = None
        if cont is None or self.urlOpener._last_waf_action:
            graph_data = self._get_movie_graphql(movieID)
            if graph_data:
                return {'data': graph_data, 'info sets': ('main',)}
            fallback = self._search_suggestion('tt%s' % movieID, 'tt', 1)
            if fallback and fallback[0][0] == movieID:
                return {'data': fallback[0][1], 'info sets': ('main',)}
            return {'data': {}, 'info sets': ('main',)}
        return self.mProxy.movie_parser.parse(cont, mdparse=self._mdparse)

    def get_movie_recommendations(self, movieID):
        # for some reason /tt0133093 is okay, but /tt0133093/ is not
        cont = self._retrieve((self.urls['movie_main'] % movieID).strip('/'))
        r = {'info sets': ('recommendations',), 'data': {}}
        ret = self.mProxy.movie_parser.parse(cont, mdparse=self._mdparse)
        if 'data' in ret and 'recommendations' in ret['data'] and ret['data']['recommendations']:
            r['data']['recommendations'] = ret['data']['recommendations']
        return r

    def get_movie_full_credits(self, movieID):
        cont = self._retrieve(self.urls['movie_main'] % movieID + 'fullcredits')
        return self.mProxy.full_credits_parser.parse(cont)

    def get_movie_plot(self, movieID):
        cont = self._retrieve(self.urls['movie_main'] % movieID + 'plotsummary',
                              _allowWaf=True)
        if self.urlOpener._last_waf_action:
            return {'data': {}, 'info sets': ('plot', 'synopsis')}
        ret = self.mProxy.plot_parser.parse(cont, getRefs=self._getRefs)
        ret['info sets'] = ('plot', 'synopsis')
        return ret

    def get_movie_awards(self, movieID):
        cont = self._retrieve(self.urls['movie_main'] % movieID + 'awards')
        return self.mProxy.movie_awards_parser.parse(cont)

    def get_movie_taglines(self, movieID):
        cont = self._retrieve(self.urls['movie_main'] % movieID + 'taglines')
        return self.mProxy.taglines_parser.parse(cont)

    def get_movie_keywords(self, movieID):
        cont = self._retrieve(self.urls['movie_main'] % movieID + 'keywords')
        return self.mProxy.keywords_parser.parse(cont)

    def get_movie_alternate_versions(self, movieID):
        cont = self._retrieve(self.urls['movie_main'] % movieID + 'alternateversions')
        return self.mProxy.alternateversions_parser.parse(cont, getRefs=self._getRefs)

    def get_movie_crazy_credits(self, movieID):
        cont = self._retrieve(self.urls['movie_main'] % movieID + 'crazycredits')
        return self.mProxy.crazycredits_parser.parse(cont, getRefs=self._getRefs)

    def get_movie_goofs(self, movieID):
        cont = self._retrieve(self.urls['movie_main'] % movieID + 'goofs')
        return self.mProxy.goofs_parser.parse(cont, getRefs=self._getRefs)

    def get_movie_quotes(self, movieID):
        cont = self._retrieve(self.urls['movie_main'] % movieID + 'quotes')
        return self.mProxy.quotes_parser.parse(cont, getRefs=self._getRefs)

    def get_movie_release_dates(self, movieID):
        cont = self._retrieve(self.urls['movie_main'] % movieID + 'releaseinfo',
                              _allowWaf=True)
        if self.urlOpener._last_waf_action:
            return self._graphql_akas_data(movieID)
        ret = self.mProxy.releasedates_parser.parse(cont)
        ret['info sets'] = ('release dates', 'akas')
        if not ret.get('data'):
            return self._graphql_akas_data(movieID)
        return ret

    get_movie_akas = get_movie_release_dates

    get_movie_release_info = get_movie_release_dates

    def get_movie_vote_details(self, movieID):
        cont = self._retrieve(self.urls['movie_main'] % movieID + 'ratings',
                              _allowWaf=True)
        if self.urlOpener._last_waf_action:
            return {'data': {}, 'info sets': ('vote details',)}
        return self.mProxy.ratings_parser.parse(cont)

    def get_movie_trivia(self, movieID):
        cont = self._retrieve(self.urls['movie_main'] % movieID + 'trivia',
                              _allowWaf=True)
        if self.urlOpener._last_waf_action:
            return self._graphql_trivia_data(movieID)
        ret = self.mProxy.trivia_parser.parse(cont, getRefs=self._getRefs)
        ret['info sets'] = ('trivia',)
        if not ret.get('data'):
            return self._graphql_trivia_data(movieID)
        return ret

    def get_movie_connections(self, movieID):
        cont = self._retrieve(self.urls['movie_main'] % movieID + 'movieconnections')
        return self.mProxy.connections_parser.parse(cont)

    def get_movie_technical(self, movieID):
        cont = self._retrieve(self.urls['movie_main'] % movieID + 'technical')
        return self.mProxy.tech_parser.parse(cont)

    def get_movie_locations(self, movieID):
        cont = self._retrieve(self.urls['movie_main'] % movieID + 'locations')
        return self.mProxy.locations_parser.parse(cont)

    def get_movie_soundtrack(self, movieID):
        cont = self._retrieve(self.urls['movie_main'] % movieID + 'soundtrack')
        return self.mProxy.soundtrack_parser.parse(cont)

    def get_movie_reviews(self, movieID):
        cont = self._retrieve(self.urls['movie_main'] % movieID + 'reviews?count=9999999&start=0',
                              _allowWaf=True)
        if self.urlOpener._last_waf_action:
            return self._graphql_reviews_data(movieID)
        ret = self.mProxy.reviews_parser.parse(cont)
        ret['info sets'] = ('reviews',)
        if not ret.get('data'):
            return self._graphql_reviews_data(movieID)
        return ret

    def get_movie_critic_reviews(self, movieID):
        cont = self._retrieve(self.urls['movie_main'] % movieID + 'criticreviews')
        return self.mProxy.criticrev_parser.parse(cont)

    def get_movie_external_reviews(self, movieID):
        cont = self._retrieve(self.urls['movie_main'] % movieID + 'externalreviews')
        return self.mProxy.externalrev_parser.parse(cont)

    def get_movie_external_sites(self, movieID):
        cont = self._retrieve(self.urls['movie_main'] % movieID + 'externalsites')
        ret = self.mProxy.externalsites_parser.parse(cont)
        ret['info sets'] = ('external sites', 'misc sites', 'sound clips',
                            'video sites', 'photo sites', 'official sites')
        return ret

    def get_movie_official_sites(self, movieID):
        cont = self._retrieve(self.urls['movie_main'] % movieID + 'officialsites')
        ret = self.mProxy.officialsites_parser.parse(cont)
        ret['info sets'] = ('external sites', 'misc sites', 'sound clips',
                            'video sites', 'photo sites', 'official sites')
        return ret

    def get_movie_misc_sites(self, movieID):
        cont = self._retrieve(self.urls['movie_main'] % movieID + 'miscsites')
        ret = self.mProxy.misclinks_parser.parse(cont)
        ret['info sets'] = ('external sites', 'misc sites', 'sound clips',
                            'video sites', 'photo sites', 'official sites')
        return ret

    def get_movie_sound_clips(self, movieID):
        cont = self._retrieve(self.urls['movie_main'] % movieID + 'soundsites')
        ret = self.mProxy.soundclips_parser.parse(cont)
        ret['info sets'] = ('external sites', 'misc sites', 'sound clips',
                            'video sites', 'photo sites', 'official sites')
        return ret

    def get_movie_video_clips(self, movieID):
        cont = self._retrieve(self.urls['movie_main'] % movieID + 'videosites')
        ret = self.mProxy.videoclips_parser.parse(cont)
        ret['info sets'] = ('external sites', 'misc sites', 'sound clips',
                            'video sites', 'photo sites', 'official sites')
        return ret

    def get_movie_photo_sites(self, movieID):
        cont = self._retrieve(self.urls['movie_main'] % movieID + 'photosites')
        ret = self.mProxy.photosites_parser.parse(cont)
        ret['info sets'] = ('external sites', 'misc sites', 'sound clips',
                            'video sites', 'photo sites', 'official sites')
        return ret

    def get_movie_news(self, movieID):
        cont = self._retrieve(self.urls['movie_main'] % movieID + 'news')
        return self.mProxy.news_parser.parse(cont, getRefs=self._getRefs)

    def _purge_seasons_data(self, data_d):
        if '_current_season' in data_d['data']:
            del data_d['data']['_current_season']
        if '_seasons' in data_d['data']:
            del data_d['data']['_seasons']
        return data_d

    def _get_movie_episodes_graphql(self, movieID, season_nums='all'):
        """Fetch series episodes from IMDb GraphQL when the episodes page is WAF-blocked."""
        imdb_id = 'tt%s' % movieID
        query = """
        query {
          title(id: "%s") {
            id
            titleText { text }
            originalTitleText { text }
            episodes {
              displayableSeasons(first: 100) {
                edges {
                  node { season }
                }
              }
              episodes(first: 250) {
                total
                edges {
                  node {
                    id
                    titleText { text }
                    releaseDate { year month day }
                    ratingsSummary { aggregateRating voteCount }
                    plot { plotText { plainText } }
                    series {
                      episodeNumber { seasonNumber episodeNumber }
                    }
                  }
                }
              }
            }
          }
        }
        """ % imdb_id
        payload = self._post_graphql(query, imdb_id)
        title = ((payload.get('data') or {}).get('title') or {})
        episodes_data = title.get('episodes') or {}
        edges = ((episodes_data.get('episodes') or {}).get('edges') or [])
        if not edges:
            return {'data': {'episodes': {}, 'number of episodes': 0}}

        if isinstance(season_nums, int):
            season_nums = {season_nums}
        elif (isinstance(season_nums, (list, tuple)) or
              not hasattr(season_nums, '__contains__')):
            season_nums = set(season_nums)

        series_title = ((title.get('originalTitleText') or {}).get('text') or
                        (title.get('titleText') or {}).get('text') or '')
        series = Movie(title=series_title, movieID=str(movieID),
                       accessSystem=self.accessSystem,
                       modFunct=self._defModFunct)
        series['kind'] = 'tv series'

        seasons = set()
        episodes = {}
        nr_eps = 0
        for edge in edges:
            node = ((edge.get('node') or {}))
            episode_id = (node.get('id') or '')
            if not episode_id.startswith('tt'):
                continue
            episode_number = (((node.get('series') or {}).get('episodeNumber') or {}))
            season = episode_number.get('seasonNumber')
            episode = episode_number.get('episodeNumber')
            if season is None or episode is None:
                continue
            try:
                season = int(season)
                episode = int(episode)
            except Exception:
                continue
            if season_nums != 'all' and season not in season_nums:
                continue

            episode_title = (node.get('titleText') or {}).get('text')
            if not episode_title:
                continue
            ep_obj = Movie(movieID=episode_id[2:], title=episode_title,
                           accessSystem=self.accessSystem,
                           modFunct=self._defModFunct)
            ep_obj['kind'] = 'episode'
            ep_obj['episode of'] = series
            ep_obj['season'] = season
            ep_obj['episode'] = episode

            release_date = node.get('releaseDate') or {}
            if release_date.get('year'):
                ep_obj['year'] = release_date['year']
                parts = [
                    str(release_date.get('year')),
                    '%02d' % release_date.get('month') if release_date.get('month') else '',
                    '%02d' % release_date.get('day') if release_date.get('day') else '',
                ]
                ep_obj['original air date'] = '-'.join([p for p in parts if p])

            ratings = node.get('ratingsSummary') or {}
            if ratings.get('aggregateRating') is not None:
                ep_obj['rating'] = ratings['aggregateRating']
            if ratings.get('voteCount') is not None:
                ep_obj['votes'] = ratings['voteCount']
            plot = ((node.get('plot') or {}).get('plotText') or {}).get('plainText')
            if plot:
                ep_obj['plot'] = plot

            episodes.setdefault(season, {})[episode] = ep_obj
            seasons.add(season)
            nr_eps += 1

        season_edges = ((episodes_data.get('displayableSeasons') or {}).get('edges') or [])
        if season_edges:
            try:
                seasons.update(
                    int((edge.get('node') or {}).get('season'))
                    for edge in season_edges
                    if (edge.get('node') or {}).get('season')
                )
            except Exception:
                pass
        return {
            'data': {
                'episodes': episodes,
                '_seasons': sorted(seasons),
                'number of episodes': nr_eps,
            }
        }

    def get_movie_episodes(self, movieID, season_nums='all'):
        try:
            cont = self._retrieve(self.urls['movie_main'] % movieID + 'episodes',
                                  _allowWaf=True)
        except IMDbDataAccessError:
            return self._get_movie_episodes_graphql(movieID, season_nums)
        if self.urlOpener._last_waf_action:
            return self._get_movie_episodes_graphql(movieID, season_nums)
        temp_d = self.mProxy.season_episodes_parser.parse(cont)
        if isinstance(season_nums, int):
            season_nums = {season_nums}
        elif (isinstance(season_nums, (list, tuple)) or
              not hasattr(season_nums, '__contains__')):
            season_nums = set(season_nums)
        if not temp_d and 'data' in temp_d:
            return {}

        _seasons = temp_d['data'].get('_seasons') or []

        nr_eps = 0
        data_d = dict()

        for season in _seasons:
            if season_nums != 'all' and season not in season_nums:
                continue
            # Prevent Critical error if season is not found #330
            try:
                cont = self._retrieve(
                    self.urls['movie_main'] % movieID + 'episodes?season=' + str(season)
                )
            except Exception:
                pass
            other_d = self.mProxy.season_episodes_parser.parse(cont)
            other_d = self._purge_seasons_data(other_d)
            other_d['data'].setdefault('episodes', {})
            # Prevent Critical error if season is not found #330
            try:
                if not (other_d and other_d['data'] and other_d['data']['episodes'][season]):
                    continue
            except Exception:
                pass
            nr_eps += len(other_d['data']['episodes'].get(season) or [])
            if data_d:
                data_d['data']['episodes'][season] = other_d['data']['episodes'][season]
            else:
                data_d = other_d
        if not data_d:
            data_d['data'] = dict()
        data_d['data']['number of episodes'] = nr_eps
        return data_d

    def get_movie_faqs(self, movieID):
        cont = self._retrieve(self.urls['movie_main'] % movieID + 'faq')
        return self.mProxy.movie_faqs_parser.parse(cont, getRefs=self._getRefs)

    def get_movie_airing(self, movieID):
        cont = self._retrieve(self.urls['movie_main'] % movieID + 'tvschedule')
        return self.mProxy.airing_parser.parse(cont)

    get_movie_tv_schedule = get_movie_airing

    def get_movie_synopsis(self, movieID):
        return self.get_movie_plot(movieID)

    def get_movie_parents_guide(self, movieID):
        cont = self._retrieve(self.urls['movie_main'] % movieID + 'parentalguide',
                              _allowWaf=True)
        if self.urlOpener._last_waf_action:
            return self._graphql_parental_guide_data(movieID)
        ret = self.mProxy.parentsguide_parser.parse(cont)
        ret['info sets'] = ('parents guide',)
        if not ret.get('data'):
            return self._graphql_parental_guide_data(movieID)
        return ret

    def _search_person(self, name, results):
        try:
            cont = self._get_search_content('nm', name, results)
        except IMDbDataAccessError:
            return self._search_suggestion(name, 'nm', results)
        data = self.spProxy.search_person_parser.parse(cont, results=results)['data']
        if data:
            return data
        return self._search_suggestion(name, 'nm', results)

    def get_person_main(self, personID):
        try:
            cont = self._retrieve(self.urls['person_main'] % personID,
                                  _allowWaf=True)
        except IMDbDataAccessError:
            cont = None
        if cont is None or self.urlOpener._last_waf_action:
            fallback = self._search_suggestion('nm%s' % personID, 'nm', 1)
            if fallback and fallback[0][0] == personID:
                return {'data': fallback[0][1], 'info sets': ('main',)}
            return {'data': {}, 'info sets': ('main',)}
        ret = self.pProxy.maindetails_parser.parse(cont)
        return ret

    def get_person_filmography(self, personID):
        cont = self._retrieve(self.urls['person_main'] % personID + 'fullcredits',
                              _allowWaf=True)
        if self.urlOpener._last_waf_action:
            return self._graphql_person_filmography_data(personID)
        ret = self.pProxy.filmo_parser.parse(cont, getRefs=self._getRefs)
        ret['info sets'] = ('filmography',)
        if not ret.get('data'):
            return self._graphql_person_filmography_data(personID)
        return ret

    def get_person_biography(self, personID):
        cont = self._retrieve(self.urls['person_main'] % personID + 'bio',
                              _allowWaf=True)
        if self.urlOpener._last_waf_action:
            return {'data': {}, 'info sets': ('biography',)}
        return self.pProxy.bio_parser.parse(cont, getRefs=self._getRefs)

    def get_person_awards(self, personID):
        cont = self._retrieve(self.urls['person_main'] % personID + 'awards')
        return self.pProxy.person_awards_parser.parse(cont)

    def get_person_other_works(self, personID):
        cont = self._retrieve(self.urls['person_main'] % personID + 'otherworks')
        return self.pProxy.otherworks_parser.parse(cont, getRefs=self._getRefs)

    def get_person_publicity(self, personID):
        cont = self._retrieve(self.urls['person_main'] % personID + 'publicity')
        return self.pProxy.publicity_parser.parse(cont)

    def get_person_official_sites(self, personID):
        cont = self._retrieve(self.urls['person_main'] % personID + 'officialsites')
        return self.pProxy.person_officialsites_parser.parse(cont)

    def get_person_news(self, personID):
        cont = self._retrieve(self.urls['person_main'] % personID + 'news')
        return self.pProxy.news_parser.parse(cont)

    def get_person_genres_links(self, personID):
        cont = self._retrieve(self.urls['person_main'] % personID + 'filmogenre')
        return self.pProxy.person_genres_parser.parse(cont)

    def get_person_keywords_links(self, personID):
        cont = self._retrieve(self.urls['person_main'] % personID + 'filmokey')
        return self.pProxy.person_keywords_parser.parse(cont)

    def _search_company(self, name, results):
        try:
            cont = self._get_search_content('co', name, results)
        except IMDbDataAccessError:
            return self._search_suggestion(name, 'co', results)
        url = self.urlOpener._last_url
        data = self.scompProxy.search_company_parser.parse(cont, url=url,
                                                           results=results)['data']
        return data or self._search_suggestion(name, 'co', results)

    def get_company_main(self, companyID):
        cont = self._retrieve(self.urls['company_main'] % companyID)
        ret = self.compProxy.company_main_parser.parse(cont)
        return ret

    def _search_keyword(self, keyword, results):
        # XXX: the IMDb web server seems to have some serious problem with
        #      non-ascii keyword.
        #      E.g.: http://www.imdb.com/keyword/fianc%E9/
        #      will return a 500 Internal Server Error: Redirect Recursion.
        try:
            cont = self._get_search_content('kw', keyword, results)
        except IMDbDataAccessError:
            self._http_logger.warn('unable to search for keyword %s', keyword,
                                   exc_info=True)
            return []
        return self.skProxy.search_keyword_parser.parse(cont, results=results)['data']

    def _get_keyword(self, keyword, results, page=None):
        try:
            url = self.urls['keyword_main'] % keyword
            if page is not None:
                url = url + "&page=" + str(page)
            cont = self._retrieve(url)
        except IMDbDataAccessError:
            self._http_logger.warn('unable to get keyword %s', keyword,
                                   exc_info=True)
            return []
        return self.skProxy.search_moviekeyword_parser.parse(cont, results=results)['data']

    def _get_top_bottom_movies(self, kind):
        if kind == 'top':
            parser = self.topBottomProxy.top250_parser
            url = self.urls['top250']
        elif kind == 'bottom':
            parser = self.topBottomProxy.bottom100_parser
            url = self.urls['bottom100']
        elif kind == 'moviemeter':
            parser = self.topBottomProxy.moviemeter100_parser
            url = self.urls['moviemeter100']
        elif kind == 'toptv':
            parser = self.topBottomProxy.toptv250_parser
            url = self.urls['toptv250']
        elif kind == 'tvmeter':
            parser = self.topBottomProxy.tvmeter100_parser
            url = self.urls['tvmeter100']
        elif kind == 'topindian250':
            parser = self.topBottomProxy.topindian250_parser
            url = self.urls['topindian250']
        elif kind == 'boxoffice':
            parser = self.topBottomProxy.boxoffice_parser
            url = self.urls['boxoffice']
        else:
            return []
        cont = self._retrieve(url)
        return parser.parse(cont)['data']

    def _get_showtimes(self):
        cont = self._retrieve(self.urls['showtimes'])
        return self.stimesProxy.showtime_parser.parse(cont)['data']

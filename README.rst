|pypi| |pyversions| |license|

.. |pypi| image:: https://img.shields.io/pypi/v/cinemagoer.svg?style=flat-square
    :target: https://pypi.org/project/cinemagoer/
    :alt: PyPI version.

.. |pyversions| image:: https://img.shields.io/pypi/pyversions/cinemagoer.svg?style=flat-square
    :target: https://pypi.org/project/cinemagoer/
    :alt: Supported Python versions.

.. |license| image:: https://img.shields.io/pypi/l/cinemagoer.svg?style=flat-square
    :target: https://github.com/cinemagoer/cinemagoer/blob/master/LICENSE.txt
    :alt: Project license.


# Cinemagoer IMDb WAF + GraphQL Compatibility Fix

> ⚠️ Notice: This is an unofficial Cinemagoer fork with IMDb WAF/GraphQL compatibility fixes. Full credit goes to the original Cinemagoer maintainers.

## Original Project

Original repository: https://github.com/cinemagoer/cinemagoer

This fork is based on the original Cinemagoer project. It adds compatibility fixes for recent IMDb website/API changes.

## Why This Fork Exists

IMDb has started blocking or changing several old HTML endpoints using AWS WAF challenges. Because of that, the original Cinemagoer package may fail while fetching movie details, posters, advanced info, search results, or episode pages.

This fork adds fallback support using IMDb JSON and GraphQL endpoints where possible.

## What Is Fixed

- Basic movie details fallback through IMDb GraphQL
- Poster / cover image support
- Rating, votes, genres, languages, runtime, and plot fallback
- IMDb suggestion JSON fallback for search
- Extra GraphQL search support for original/localized titles
- Better typo/spelling search scoring
- TV movie search support
- TV mini series, TV episode, video game, and related title type mapping
- AKAs fallback
- Trivia fallback
- Reviews fallback
- Parents guide fallback
- Person filmography fallback
- `get_movie_episodes(movieID)` fallback when IMDb episode pages are blocked by AWS WAF

## How It Works

This fork keeps the original Cinemagoer behavior first wherever possible.

When IMDb HTML pages work normally, Cinemagoer continues to use the normal parser.

When IMDb returns an AWS WAF challenge or the old page cannot be parsed, this fork uses fallback methods:

- IMDb suggestion JSON for search results
- IMDb GraphQL for movie details
- IMDb GraphQL for advanced title info
- IMDb GraphQL for person filmography
- IMDb GraphQL for episode lists when `/episodes` is WAF blocked

This means existing code like this can continue working:

```python
import imdb

ia = imdb.Cinemagoer()
movie = ia.get_movie("0499549")
print(movie.get("title"))
```

## Search Improvements

Some typo searches previously returned unrelated results first.

Example typo:

```text
a slicee of romance
```

Now closer matches are ranked higher:

```text
A Slice of Romance
A Slice of Chicago Romance
```

This is useful for Telegram bots, auto-filter bots, and spelling suggestion systems.

Example:

```python
import imdb

ia = imdb.Cinemagoer()
results = ia.search_movie("a slicee of romance", results=10)

print([(m.movieID, m.get("title"), m.get("kind"), m.get("year")) for m in results])
```

Expected output:

```python
[
    ('14599718', 'A Slice of Romance', 'tv movie', 2021),
    ('15821952', 'A Slice of Chicago Romance', 'tv movie', 2022),
    ...
]
```

## TV Movie Support

IMDb suggestion JSON can return title types such as:

```text
tvmovie
tvseries
tvminiseries
tvepisode
videogame
```

This fork normalizes those into Cinemagoer-style kinds:

```text
tv movie
tv series
tv mini series
episode
video game
```

Example:

```python
import imdb

ia = imdb.Cinemagoer()
results = ia.search_movie("A Slice of Chicago Romance", results=5)

print([(m.movieID, m.get("title"), m.get("kind"), m.get("year")) for m in results])
```

Expected output:

```python
[('15821952', 'A Slice of Chicago Romance', 'tv movie', 2022), ...]
```

## Episode Support

The original IMDb episode page can fail with an AWS WAF challenge.

Example blocked URL:

```text
https://www.imdb.com/title/tt0903747/episodes
```

This fork detects the WAF challenge and uses a GraphQL fallback for:

```python
ia.get_movie_episodes(movieID)
```

Example:

```python
import imdb

ia = imdb.Cinemagoer()
eps = ia.get_movie_episodes("0903747")

data = eps.get("data", {})
print(data.get("number of episodes"))
print(sorted((data.get("episodes") or {}).keys())[:5])

first = data["episodes"][1][1]
print(first.movieID, first.get("title"), first.get("season"), first.get("episode"), first.get("rating"))
```

Expected output:

```text
62
[1, 2, 3, 4, 5]
0959621 Pilot 1 1 9
```

## Basic Movie Details Example

```python
import imdb

ia = imdb.Cinemagoer()
movie = ia.get_movie("0499549")

print(movie.get("title"))
print(movie.get("year"))
print(movie.get("kind"))
print(movie.get("rating"))
print(movie.get("votes"))
print(movie.get("genres"))
print(movie.get("languages"))
print(movie.get("runtimes"))
print(movie.get("plot"))
print(movie.get("full-size cover url"))
```

Example output:

```text
Avatar
2009
movie
7.9
1496830
['Action', 'Adventure', 'Fantasy', 'Sci-Fi']
['English', 'Spanish']
['162']
A paraplegic Marine dispatched to the moon Pandora...
True
```

## Advanced Info Example

```python
import imdb

ia = imdb.Cinemagoer()
movie = ia.get_movie("0499549", info=["akas", "trivia", "reviews", "parents guide"])

print("akas:", len(movie.get("akas") or []))
print("trivia:", len(movie.get("trivia") or []))
print("reviews:", len(movie.get("reviews") or []))
print("parents:", [x for x in movie.keys() if str(x).startswith("advisory ")])
```

Example output:

```text
akas: 65
trivia: 50
reviews: 50
parents: ['advisory sex and nudity', 'advisory votes', 'advisory violence and gore', 'advisory profanity', 'advisory alcohol, drugs and smoking', 'advisory frightening and intense scenes']
```

## Installation

Install directly from this repository:

```bash
pip uninstall -y cinemagoer imdbpy
pip install --no-cache-dir --force-reinstall git+https://github.com/YOUR_USERNAME/YOUR_REPO_NAME.git
```

Example:

```bash
pip uninstall -y cinemagoer imdbpy
pip install --no-cache-dir --force-reinstall git+https://github.com/betterbot69/cinemagoer.git
```

## Update Existing Installation

If you already installed this fork and want to update it:

```bash
pip install --no-cache-dir --force-reinstall git+https://github.com/betterbot69/cinemagoer.git
```

If the old package is still being used, uninstall first:

```bash
pip uninstall -y cinemagoer imdbpy
pip install --no-cache-dir --force-reinstall git+https://github.com/betterbot69/cinemagoer.git
```

## Test Commands

Basic movie details:

```bash
python -c "import imdb; ia=imdb.Cinemagoer(); m=ia.get_movie('0499549'); print(m.get('title'), m.get('year'), m.get('rating'), m.get('votes'), m.get('genres'), m.get('languages'), m.get('runtimes'), bool(m.get('full-size cover url')))"
```

TV movie search:

```bash
python -c "import imdb; ia=imdb.Cinemagoer(); r=ia.search_movie('A Slice of Chicago Romance', results=5); print([(m.movieID, m.get('title'), m.get('kind'), m.get('year')) for m in r])"
```

Typo search:

```bash
python -c "import imdb; ia=imdb.Cinemagoer(); r=ia.search_movie('a slicee of romance', results=10); print([(m.movieID, m.get('title'), m.get('kind'), m.get('year')) for m in r])"
```

Episode fallback:

```bash
python -c "import imdb; ia=imdb.Cinemagoer(); eps=ia.get_movie_episodes('0903747'); data=eps.get('data',{}); print(data.get('number of episodes'), sorted((data.get('episodes') or {}).keys())[:5]); s=(data.get('episodes') or {}).get(1,{}); first=s.get(1); print(first.movieID if first else None, first.get('title') if first else None, first.get('season') if first else None, first.get('episode') if first else None, first.get('rating') if first else None)"
```

Advanced info:

```bash
python -c "import imdb; ia=imdb.Cinemagoer(); m=ia.get_movie('0499549', info=['akas','trivia','reviews','parents guide']); print('akas', len(m.get('akas') or [])); print('trivia', len(m.get('trivia') or [])); print('reviews', len(m.get('reviews') or [])); print('parents', [x for x in m.keys() if str(x).startswith('advisory ')])"
```

Check installed package path:

```bash
python -c "import imdb; print(imdb.__file__)"
```

## Notes For Bot Developers

If you use this fork in a Telegram bot or auto-filter bot, make sure your bot does not filter out TV movies.

For example, if your bot has code like this:

```python
movieid = list(filter(lambda k: k.get('kind') in ['movie', 'tv series'], filtered))
```

Change it to:

```python
movieid = list(filter(lambda k: k.get('kind') in ['movie', 'tv movie', 'tv series', 'tv mini series'], filtered))
```

Without this change, Cinemagoer may return TV movies correctly, but your bot can still hide them from suggestions.

## Heroku Usage

If your app uses `requirements.txt`, you can install this fork like this:

```txt
git+https://github.com/betterbot69/cinemagoer.git
```

If `cinemagoer` or `IMDbPY` is already listed, remove the old package entry and use only the GitHub fork entry.

Example:

```txt
git+https://github.com/betterbot69/cinemagoer.git
```

Then redeploy your Heroku app.

## VPS Usage

On VPS, activate your virtual environment and reinstall:

```bash
source venv/bin/activate
pip uninstall -y cinemagoer imdbpy
pip install --no-cache-dir --force-reinstall git+https://github.com/betterbot69/cinemagoer.git
```

Then restart your bot or application.

Example:

```bash
sudo systemctl restart your-bot-service
```

Or if you use screen/tmux, stop and start the bot again.

## Important Notes

- This is an unofficial fork.
- Full credit goes to the original Cinemagoer project and maintainers.
- IMDb can change its website, GraphQL schema, JSON endpoints, or WAF rules at any time.
- This fork improves compatibility with the current IMDb behavior.
- No IMDb scraper can be guaranteed to work forever.
- If IMDb changes again in the future, this fork may need new patches.

## Disclaimer

This project is only for compatibility and educational purposes. Please respect IMDb's terms and use responsibly.


**Cinemagoer** (previously known as *IMDbPY*) is a Python package for retrieving and managing the data
of the `IMDb`_ movie database about movies, people and companies.

This project and its authors are not affiliated in any way to Internet Movie Database Inc.; see the `DISCLAIMER.txt`_ file for details about data licenses.

.. admonition:: Revamp notice
   :class: note

   Starting on November 2017, many things were improved and simplified:

   - moved the package to Python 3 (compatible with Python 2.7)
   - removed dependencies: SQLObject, C compiler, BeautifulSoup
   - removed the "mobile" and "httpThin" parsers
   - introduced a test suite (`please help with it!`_)


Main features
-------------

- written in Python 3 (compatible with Python 2.7)

- platform-independent

- simple and complete API

- released under the terms of the GPL 2 license

Cinemagoer powers many other software and has been used in various research papers.
`Curious about that`_?


Installation
------------

Whenever possible, please use the latest version from the repository::

   pip install git+https://github.com/betterbot69/cinemagoer


But if you want, you can also install the latest release from PyPI::

   pip install cinemagoer


Example
-------

Here's an example that demonstrates how to use Cinemagoer:

.. code-block:: python

   from imdb import Cinemagoer

   # create an instance of the Cinemagoer class
   ia = Cinemagoer()

   # get a movie
   movie = ia.get_movie('0133093')

   # print the names of the directors of the movie
   print('Directors:')
   for director in movie['directors']:
       print(director['name'])

   # print the genres of the movie
   print('Genres:')
   for genre in movie['genres']:
       print(genre)

   # search for a person name
   people = ia.search_person('Mel Gibson')
   for person in people:
      print(person.personID, person['name'])


Getting help
------------

Please refer to the `support`_ page on the `project homepage`_
and to the the online documentation on `Read The Docs`_.

The sources are available on `GitHub`_.

Contribute
------------

Visit the `CONTRIBUTOR_GUIDE.rst`_ to learn how you can contribute to the Cinemagoer package.

License
-------

Copyright (C) 2004-2022 Davide Alberani <da --> mimante.net> et al.

Cinemagoer is released under the GPL license, version 2 or later.
Read the included `LICENSE.txt`_ file for details.

NOTE: For a list of persons who share the copyright over specific portions of code, see the `CONTRIBUTORS.txt`_ file.

NOTE: See also the recommendations in the `DISCLAIMER.txt`_ file.

.. _IMDb: https://www.imdb.com/
.. _please help with it!: http://cinemagoer.readthedocs.io/en/latest/devel/test.html
.. _Curious about that: https://cinemagoer.github.io/ecosystem/
.. _project homepage: https://cinemagoer.github.io/
.. _support: https://cinemagoer.github.io/support/
.. _Read The Docs: https://cinemagoer.readthedocs.io/
.. _GitHub: https://github.com/cinemagoer/cinemagoer
.. _CONTRIBUTOR_GUIDE.rst: https://github.com/ethorne2/cinemagoer/blob/documentation-add-contributor-guide/CONTRIBUTOR_GUIDE.rst
.. _LICENSE.txt: https://raw.githubusercontent.com/cinemagoer/cinemagoer/master/LICENSE.txt
.. _CONTRIBUTORS.txt: https://raw.githubusercontent.com/cinemagoer/cinemagoer/master/CONTRIBUTORS.txt
.. _DISCLAIMER.txt: https://raw.githubusercontent.com/cinemagoer/cinemagoer/master/DISCLAIMER.txt

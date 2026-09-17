import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import ratings


def response(payload):
    result = MagicMock()
    result.__enter__.return_value = result
    result.read.return_value = json.dumps(payload).encode()
    return result


MOVIE = {
    "title": "The Matrix", "year": 1999, "type": "movie",
    "ids": {"imdb": "tt0133093", "mdblist": "a2na"},
    "ratings": [
        {"source": "imdb", "value": 8.7, "score": 87},
        {"source": "tomatoes", "value": 83, "score": 83, "url": "/m/the_matrix"},
        {"source": "popcorn", "value": 99, "score": 99},
    ],
}
SEARCH = {
    "search": [{
        "title": "The Matrix", "year": 1999, "type": "movie",
        "ids": {"imdbid": "tt0133093", "mdblist": "a2na"},
    }],
    "total": 1,
}


def replies(get, *, movie=None, search=None):
    get.side_effect = [
        response(SEARCH if search is None else search),
        response(MOVIE if movie is None else movie),
    ]


class LookupTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"MDBLIST_API_KEY": "testapikey"}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def lookup(self, title="The Matrix", year="1999", show_type="movie"):
        return ratings.fetch_ratings(title, year, show_type)

    @patch("ratings.urlopen")
    def test_fetches_source_values_not_normalised_or_audience_scores(self, get):
        replies(get)
        result = self.lookup()
        self.assertEqual(result, ratings.MovieRatings(
            "8.7/10", "83%", "tt0133093", "https://www.rottentomatoes.com/m/the_matrix",
        ))
        search_request, detail_request = [call.args[0] for call in get.call_args_list]
        self.assertEqual(urlsplit(search_request.full_url).scheme, "https")
        self.assertEqual(urlsplit(search_request.full_url).path, "/search/movie")
        self.assertEqual(parse_qs(urlsplit(search_request.full_url).query), {
            "apikey": ["testapikey"], "query": ["The Matrix"],
            "year": ["1999"], "limit": ["30"],
        })
        self.assertEqual(urlsplit(detail_request.full_url).path, "/imdb/movie/tt0133093")
        self.assertTrue(all(call.kwargs["timeout"] == 10 for call in get.call_args_list))

    @patch("ratings.urlopen")
    def test_filters_the_providers_plus_minus_one_year_hint(self, get):
        wrong_year = {**SEARCH["search"][0], "year": 1998}
        replies(get, search={"search": [wrong_year, SEARCH["search"][0]], "total": 2})
        self.assertEqual(self.lookup().imdb, "8.7/10")

    @patch("ratings.urlopen")
    def test_yearless_title_requires_one_exact_search_match(self, get):
        replies(get)
        self.assertEqual(self.lookup(year=None).imdb, "8.7/10")
        query = parse_qs(urlsplit(get.call_args_list[0].args[0].full_url).query)
        self.assertNotIn("year", query)

    @patch("ratings.urlopen")
    def test_anime_episode_label_falls_back_to_exact_parent_series(self, get):
        series_search = {
            "search": [{
                "title": "Chainsmoker Cat", "year": 2026, "type": "show",
                "ids": {"imdbid": "tt39551330", "mdblist": "448ly"},
            }],
            "total": 1,
        }
        series = {
            "title": "Chainsmoker Cat", "year": 2026, "type": "show",
            "ids": {"imdb": "tt39551330", "mdblist": "448ly"},
            "ratings": [{"source": "imdb", "value": 6.9}],
        }
        get.side_effect = [
            response({"search": [], "total": 0}),
            response(series_search),
            response(series),
        ]

        result = self.lookup(
            "Chainsmoker Cat episode 10", "2026", "anime")

        self.assertEqual(result.imdb, "6.9/10")
        searches = [
            parse_qs(call.args[0].full_url.split("?", 1)[1])["query"][0]
            for call in get.call_args_list[:2]
        ]
        self.assertEqual(
            searches, ["Chainsmoker Cat episode 10", "Chainsmoker Cat"])
        self.assertEqual(
            urlsplit(get.call_args_list[0].args[0].full_url).path,
            "/search/any",
        )

    def test_movie_titles_are_never_rewritten_as_episode_labels(self):
        self.assertEqual(
            ratings._search_titles("Episode 10", "movie"),
            ["Episode 10"],
        )

    @patch("ratings.urlopen")
    def test_yearless_remakes_are_not_guessed(self, get):
        get.return_value = response({
            "total": 2, "search": [
                {**SEARCH["search"][0], "title": "The Thing", "year": 1982},
                {**SEARCH["search"][0], "title": "The Thing", "year": 2011},
            ],
        })
        with self.assertRaisesRegex(ratings.RatingsError, "release year"):
            self.lookup("The Thing", None)
        self.assertEqual(get.call_count, 1)

    @patch("ratings.urlopen")
    def test_truncated_search_results_are_not_assumed_unique(self, get):
        get.return_value = response({**SEARCH, "total": 40})
        with self.assertRaisesRegex(ratings.RatingsError, "incomplete"):
            self.lookup()

    @patch("ratings.urlopen")
    def test_wrong_detail_identity_is_rejected(self, get):
        for changed in (
            {"title": "Another Film"}, {"year": 2003}, {"type": "show"},
            {"ids": {"imdb": "tt1111111"}}, {"year": None}, {"year": True},
        ):
            with self.subTest(changed=changed):
                replies(get, movie={**MOVIE, **changed})
                with self.assertRaisesRegex(ratings.RatingsError, "unverified title"):
                    self.lookup()

    @patch("ratings.urlopen")
    def test_native_id_can_resolve_a_title_without_an_imdb_id(self, get):
        search = {"total": 1, "search": [{**SEARCH["search"][0], "ids": {"mdblist": "a2na"}}]}
        replies(get, search=search, movie={**MOVIE, "ids": {"mdblist": "a2na"}})
        result = self.lookup()
        self.assertIsNone(result.imdb_id)
        self.assertEqual(result.rotten_tomatoes, "83%")
        self.assertEqual(urlsplit(get.call_args.args[0].full_url).path, "/mdblist/movie/a2na")

    @patch("ratings.urlopen")
    def test_title_punctuation_and_article_are_normalised(self, get):
        search = {"total": 1, "search": [{**SEARCH["search"][0], "title": "The Matrix: Reloaded", "year": 2003}]}
        replies(get, search=search, movie={**MOVIE, "title": "The Matrix: Reloaded", "year": 2003})
        self.assertEqual(self.lookup("Matrix Reloaded", "2003").imdb, "8.7/10")

    @patch("ratings.urlopen")
    def test_tv_uses_show_endpoints(self, get):
        replies(get, search={"total": 1, "search": [{**SEARCH["search"][0], "type": "show"}]},
                movie={**MOVIE, "type": "show"})
        self.assertEqual(self.lookup(show_type="tv").rotten_tomatoes, "83%")
        self.assertEqual(urlsplit(get.call_args_list[0].args[0].full_url).path, "/search/show")
        self.assertEqual(urlsplit(get.call_args.args[0].full_url).path, "/imdb/show/tt0133093")

    @patch("ratings.urlopen")
    def test_anime_can_resolve_a_movie_or_show(self, get):
        replies(get)
        self.assertEqual(self.lookup(show_type="anime").imdb, "8.7/10")
        self.assertEqual(urlsplit(get.call_args_list[0].args[0].full_url).path, "/search/any")

    @patch("ratings.urlopen")
    def test_missing_critic_score_never_falls_back_to_popcorn_or_composite(self, get):
        replies(get, movie={**MOVIE, "score": 99, "ratings": [
            {"source": "imdb", "value": 8.7, "score": 87},
            {"source": "tomatoes", "value": None, "score": 88},
            {"source": "popcorn", "value": 99},
            {"source": "tmdb", "value": 90},
        ]})
        result = self.lookup()
        self.assertIsNone(result.rotten_tomatoes)
        self.assertEqual(result.imdb, "8.7/10")
        self.assertEqual(result.missing_message(), "No Rotten Tomatoes score is available.")

    @patch("ratings.urlopen")
    def test_missing_scores_and_real_zero_percent_are_distinct(self, get):
        replies(get, movie={**MOVIE, "ratings": [{"source": "tomatoes", "value": 0}]})
        result = self.lookup()
        self.assertEqual(result.rotten_tomatoes, "0%")
        self.assertIsNone(result.imdb)
        self.assertIn("IMDb", result.missing_message())
        replies(get, movie={**MOVIE, "ratings": []})
        self.assertIn("IMDb or Rotten Tomatoes", self.lookup().missing_message())

    @patch("ratings.urlopen")
    def test_omdb_is_preferred_when_it_returns_verified_scores(self, get):
        get.return_value = response({
            "Response": "True",
            "Title": "The Matrix",
            "Year": "1999",
            "Type": "movie",
            "imdbID": "tt0133093",
            "imdbRating": "8.7",
            "Ratings": [
                {"Source": "Internet Movie Database", "Value": "8.7/10"},
                {"Source": "Rotten Tomatoes", "Value": "83%"},
            ],
        })

        result = ratings.fetch_ratings(
            "The Matrix", "1999", "movie",
            api_key="mdblist-key", omdb_api_key="omdb-key")

        self.assertEqual(
            result,
            ratings.MovieRatings(
                "8.7/10", "83%", "tt0133093", None, "OMDb"),
        )
        self.assertEqual(get.call_count, 1)
        query = parse_qs(urlsplit(get.call_args.args[0].full_url).query)
        self.assertEqual(query["t"], ["The Matrix"])
        self.assertEqual(query["type"], ["movie"])
        self.assertIn(
            "Ratings via [OMDb]",
            ratings.discord_announcement(
                "Body", 120, result, limit=500),
        )

    @patch("ratings.urlopen")
    def test_omdb_without_scores_falls_back_to_mdblist(self, get):
        series_search = {
            "search": [{
                "title": "Chainsmoker Cat", "year": 2026, "type": "show",
                "ids": {"imdbid": "tt39551330", "mdblist": "448ly"},
            }],
            "total": 1,
        }
        series = {
            "title": "Chainsmoker Cat", "year": 2026, "type": "show",
            "ids": {"imdb": "tt39551330", "mdblist": "448ly"},
            "ratings": [{"source": "imdb", "value": 6.9}],
        }
        get.side_effect = [
            response({"Response": "False", "Error": "Series not found!"}),
            response({
                "Response": "True", "Title": "Chainsmoker Cat",
                "Year": "2026–", "Type": "series", "imdbID": "tt39551330",
                "imdbRating": "N/A", "Ratings": [],
            }),
            response({"search": [], "total": 0}),
            response(series_search),
            response(series),
        ]

        result = ratings.fetch_ratings(
            "Chainsmoker Cat episode 10", "2026", "anime",
            api_key="mdblist-key", omdb_api_key="omdb-key")

        self.assertEqual(result.imdb, "6.9/10")
        self.assertIsNone(result.rotten_tomatoes)

    @patch("ratings.urlopen")
    def test_invalid_scores_and_conflicts_are_rejected(self, get):
        cases = [
            [{"source": "imdb", "value": 11}],
            [{"source": "imdb", "value": -1}],
            [{"source": "imdb", "value": True}],
            [{"source": "imdb", "value": float("nan")}],
            [{"source": "tomatoes", "value": 101}],
            [{"source": "tomatoes", "value": 83.5}],
            [{"source": "tomatoes", "value": "@everyone"}],
            [{"source": "tomatoes", "value": 83}, {"source": "tomatoes", "value": 84}],
            None,
        ]
        for entries in cases:
            with self.subTest(entries=entries):
                replies(get, movie={**MOVIE, "ratings": entries})
                with self.assertRaises(ratings.RatingsError):
                    self.lookup()

    def test_rotten_tomatoes_links_use_only_the_real_site(self):
        self.assertEqual(ratings._rotten_url("/m/toy_story"),
                         "https://www.rottentomatoes.com/m/toy_story")
        self.assertEqual(ratings._rotten_url("https://www.rottentomatoes.com/m/toy_story?tracking=x"),
                         "https://www.rottentomatoes.com/m/toy_story")
        self.assertEqual(ratings._rotten_url("/tv/good_omens/s01"),
                         "https://www.rottentomatoes.com/tv/good_omens/s01")
        self.assertIsNone(ratings._rotten_url(None))
        for value in ("https://example.com/m/toy_story", "//example.com/m/test",
                      "javascript:alert(1)", 123, "/m/test\n@everyone",
                      "https://[invalid", "/m/" + "x" * 500):
            with self.assertRaises(ratings.RatingsError):
                ratings._rotten_url(value)

    @patch("ratings.urlopen")
    def test_provider_errors_are_safe_to_display(self, get):
        for field in ("error", "detail"):
            get.return_value = response({field: "Unexpected error containing testapikey"})
            with self.assertRaises(ratings.RatingsError) as caught:
                self.lookup()
            self.assertNotIn("testapikey", str(caught.exception))

    @patch("ratings.urlopen")
    def test_http_and_network_errors_do_not_leak_key(self, get):
        for error in (
            HTTPError("https://api.mdblist.com/?apikey=testapikey", 401, "bad key", {}, None),
            HTTPError("https://api.mdblist.com/?apikey=testapikey", 429, "limit", {}, None),
            URLError("testapikey"), TimeoutError("testapikey"),
        ):
            with self.subTest(error=type(error).__name__):
                get.side_effect = error
                with self.assertRaises(ratings.RatingsError) as caught:
                    self.lookup()
                self.assertNotIn("testapikey", str(caught.exception))

    @patch("ratings.urlopen")
    def test_invalid_or_oversized_response_is_rejected(self, get):
        for raw in (b"not json", b"[]", b"x" * (ratings.MAX_RESPONSE_BYTES + 1)):
            with self.subTest(size=len(raw)):
                mock_response = response({})
                mock_response.read.return_value = raw
                get.return_value = mock_response
                with self.assertRaises(ratings.RatingsError):
                    self.lookup()

    @patch("ratings.urlopen")
    def test_invalid_input_does_not_contact_provider(self, get):
        for title, year in (("", "1999"), ("The Matrix", "abcd"), ("The Matrix", "99")):
            with self.subTest(title=title, year=year):
                with self.assertRaises(ratings.RatingsError):
                    self.lookup(title, year)
        get.assert_not_called()

    @patch("ratings.load_api_key")
    @patch("ratings.urlopen")
    def test_preloaded_key_avoids_reopening_secret_store(self, get, load):
        replies(get)
        self.assertEqual(ratings.fetch_ratings("The Matrix", "1999", api_key="memory-key").imdb, "8.7/10")
        load.assert_not_called()


class CredentialTests(unittest.TestCase):
    @patch("ratings.subprocess.run")
    def test_pass_reads_the_first_line_without_prompting(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "private-key\nNotes\n", "")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ratings.load_api_key(), "private-key")
        self.assertEqual(run.call_args.args[0], ["pass", "show", "--", "api/mdblist"])
        self.assertIn("--pinentry-mode error", run.call_args.kwargs["env"]["PASSWORD_STORE_GPG_OPTS"])
        self.assertTrue(run.call_args.kwargs["capture_output"])

    @patch("ratings.subprocess.run")
    def test_environment_key_does_not_invoke_pass(self, run):
        with patch.dict(os.environ, {"MDBLIST_API_KEY": "environment-key"}, clear=True):
            self.assertEqual(ratings.load_api_key(), "environment-key")
        run.assert_not_called()

    @patch("ratings.subprocess.run")
    def test_pass_failures_do_not_expose_output(self, run):
        with patch.dict(os.environ, {}, clear=True):
            run.return_value = subprocess.CompletedProcess([], 1, "secret", "secret")
            with self.assertRaises(ratings.RatingsError) as caught:
                ratings.load_api_key()
            self.assertNotIn("secret", str(caught.exception))
            run.side_effect = subprocess.TimeoutExpired("pass", 10, output="secret")
            with self.assertRaises(ratings.RatingsError) as caught:
                ratings.load_api_key()
            self.assertNotIn("secret", str(caught.exception))

    @patch("ratings.subprocess.run")
    def test_empty_pass_entry_is_not_a_request_to_list_the_store(self, run):
        with patch.dict(os.environ, {"MDBLIST_PASS_ENTRY": ""}, clear=True):
            with self.assertRaisesRegex(ratings.RatingsError, "entry name is empty"):
                ratings.load_api_key()
        run.assert_not_called()

    def test_optional_private_file_and_multiline_rejection(self):
        temp_root = Path(__file__).resolve().parents[1] / "tmp"
        temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root) as directory:
            key = Path(directory) / "mdblist.key"
            with patch.dict(os.environ, {"MDBLIST_API_KEY_FILE": str(key)}, clear=True):
                with self.assertRaisesRegex(ratings.RatingsError, "could not be read"):
                    ratings.load_api_key()
                key.write_text("file-key\n")
                self.assertEqual(ratings.load_api_key(), "file-key")
                key.write_text("first\nsecond\n")
                with self.assertRaisesRegex(ratings.RatingsError, "one non-empty"):
                    ratings.load_api_key()


class FormattingTests(unittest.TestCase):
    scores = ratings.MovieRatings("8.7/10", "83%", "tt0133093",
                                 "https://www.rottentomatoes.com/m/the_matrix")

    def test_scores_follow_runtime_and_preserve_surrounding_text(self):
        body = "**Film:** The Matrix\n**Runtime:** about 2h 16m\n\n**Plot**\nSynopsis."
        result = ratings.discord_announcement(body, 136, self.scores, limit=1800)
        self.assertIn("**Runtime:** about 2h 16m\n:star: **IMDb:**", result)
        self.assertLess(result.index("IMDb"), result.index("Rotten Tomatoes"))
        self.assertIn("Synopsis.", result)
        self.assertIn("https://www.imdb.com/title/tt0133093/", result)
        self.assertIn("https://www.rottentomatoes.com/m/the_matrix", result)
        self.assertIn("Ratings via [MDBList]", result)

    def test_fallback_adds_runtime_when_ai_omits_it(self):
        result = ratings.discord_announcement("Movie details.", 136, self.scores, limit=1800)
        self.assertIn("**Runtime:** about 2h 16m\n:star: **IMDb:**", result)

    def test_long_copy_keeps_both_scores_and_reserved_footer(self):
        prefix = "<@&123456789012345678>\n"
        footer = "\n\n:calendar_spiral: **Event:** https://discord.com/events/123456789012345678/987654321098765432"
        body = "Long introduction. " * 300 + "\n**Runtime:** 2h\n" + "Long synopsis. " * 300
        result = prefix + ratings.discord_announcement(
            body, 120, self.scores, limit=2000-len(prefix)-len(footer),
        ) + footer
        self.assertLessEqual(len(result), 2000)
        self.assertTrue(result.endswith(footer))
        self.assertIn("8.7/10", result)
        self.assertIn("83%", result)

    def test_malformed_long_runtime_line_cannot_consume_the_budget(self):
        result = ratings.discord_announcement("Runtime: " + "x" * 5000, 136, self.scores, limit=1800)
        self.assertLessEqual(len(result), 1800)
        self.assertIn("**Runtime:** about 2h 16m", result)
        self.assertIn("83%", result)

    def test_event_scores_fit_without_truncating_the_header(self):
        result = ratings.discord_event_description("A synopsis. " * 300, 136, self.scores)
        self.assertLessEqual(len(result), 1000)
        self.assertTrue(result.startswith("Runtime: about 2h 16m\nIMDb: 8.7/10"))
        self.assertIn("Rotten Tomatoes (critics): 83%", result)
        self.assertIn("Ratings via MDBList", result)

    def test_event_reuses_the_announcement_runtime_not_a_different_default(self):
        result = ratings.discord_event_description(
            "Synopsis.", 120, self.scores,
            announcement="**Film:** The Matrix\n:hourglass_flowing_sand: **Runtime:** about 2h 16m",
        )
        self.assertTrue(result.startswith("Runtime: about 2h 16m\nIMDb: 8.7/10"))

    def test_missing_scores_are_explicit(self):
        result = ratings.discord_event_description("Synopsis.", 90, ratings.MovieRatings())
        self.assertIn("IMDb: unavailable", result)
        self.assertIn("Rotten Tomatoes (critics): unavailable", result)
        self.assertNotIn("0/10", result)

    def test_insufficient_output_budget_is_not_silent(self):
        with self.assertRaises(ValueError):
            ratings.discord_announcement("Body", 136, self.scores, limit=10)


if __name__ == "__main__":
    unittest.main()

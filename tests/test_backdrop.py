import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from PIL import Image

import find_backdrop as backdrop


def response(data):
    result = MagicMock()
    result.__enter__.return_value = result
    result.read.return_value = data if isinstance(data, bytes) else json.dumps(data).encode()
    result.read1 = None
    return result


def image(width=640, height=360, format="JPEG", color="blue"):
    output = io.BytesIO()
    Image.new("RGB", (width, height), color).save(output, format=format)
    return output.getvalue()


MOVIE = {"id": 13156, "title": "Secondhand Lions", "release_date": "2003-09-19"}
IMAGES = {"backdrops": [
    {"file_path": "/landscape.jpg", "width": 1920, "height": 1080, "iso_639_1": None},
    {"file_path": "/poster.jpg", "width": 500, "height": 750, "iso_639_1": "en"},
]}


class BackdropTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"TMDB_READ_ACCESS_TOKEN": "test-token"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        temp_root = Path(__file__).resolve().parents[1] / "tmp"
        temp_root.mkdir(exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=temp_root)
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def fetch(self, title="Secondhand Lions", year="2003", show_type="movie", imdb_id=None,
              imdb_id_explicit=True):
        return backdrop.fetch_backdrop(
            title, year, show_type, directory=self.directory, imdb_id=imdb_id,
            imdb_id_explicit=imdb_id_explicit,
        )

    @patch("find_backdrop.urlopen")
    def test_api_authentication_and_real_validated_16_by_9_image(self, get):
        get.side_effect = [response({"results": [MOVIE]}), response(IMAGES), response(image())]
        path = self.fetch()
        with Image.open(path) as result:
            self.assertEqual(result.size, (1280, 720))
            self.assertEqual(result.format, "JPEG")
        request = get.call_args_list[0].args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(parse_qs(urlsplit(request.full_url).query), {
            "query": ["Secondhand Lions"], "year": ["2003"], "include_adult": ["false"],
        })
        self.assertEqual(urlsplit(request.full_url).path, "/3/search/movie")
        self.assertEqual(urlsplit(get.call_args_list[1].args[0].full_url).path, "/3/movie/13156/images")
        cdn_request = get.call_args_list[2].args[0]
        self.assertEqual(cdn_request.full_url, backdrop.IMAGE_BASE + "/landscape.jpg")
        self.assertIsNone(cdn_request.get_header("Authorization"))
        self.assertTrue(all(call.kwargs["timeout"] == backdrop.TIMEOUT for call in get.call_args_list))

    @patch("find_backdrop.urlopen")
    def test_cached_image_works_without_network_or_credential(self, get):
        get.side_effect = [response({"results": [MOVIE]}), response(IMAGES), response(image())]
        path = self.fetch()
        get.reset_mock()
        with patch("find_backdrop.load_credential", side_effect=AssertionError("Credential read")):
            self.assertEqual(self.fetch(), path)
        get.assert_not_called()

    @patch("find_backdrop.urlopen")
    def test_corrupt_cache_is_replaced_and_reported(self, get):
        get.side_effect = [response({"results": [MOVIE]}), response(IMAGES), response(image())] * 2
        path = self.fetch()
        path.write_bytes(b"not an image")
        with patch("sys.stderr", new_callable=io.StringIO) as errors:
            self.assertEqual(self.fetch(), path)
        self.assertIn("cached backdrop is invalid", errors.getvalue())
        with Image.open(path) as result:
            result.load()

    @patch("find_backdrop.urlopen")
    def test_rejects_wrong_year_or_unrelated_title(self, get):
        for item in ({**MOVIE, "release_date": "2004-01-01"}, {**MOVIE, "title": "Another Film"}):
            get.return_value = response({"results": [item]})
            with self.assertRaisesRegex(backdrop.BackdropError, "No exact"):
                self.fetch()

    @patch("find_backdrop.urlopen")
    def test_ambiguous_yearless_title_does_not_choose_first_result(self, get):
        get.return_value = response({"results": [MOVIE, {**MOVIE, "id": 999}]})
        with self.assertRaisesRegex(backdrop.BackdropError, "Multiple"):
            self.fetch(year=None)

    @patch("find_backdrop.urlopen")
    def test_tv_episode_uses_parent_series_and_tv_api(self, get):
        get.side_effect = [
            response({"results": []}),
            response({"results": [{"id": 123, "name": "My Series", "first_air_date": "2020-01-01"}]}),
            response(IMAGES), response(image()),
        ]
        self.fetch("My Series episode 10", "2020", "tv")
        self.assertEqual(urlsplit(get.call_args_list[1].args[0].full_url).path, "/3/search/tv")
        params = parse_qs(urlsplit(get.call_args_list[1].args[0].full_url).query)
        self.assertEqual(params["query"], ["My Series"])
        self.assertEqual(params["first_air_date_year"], ["2020"])
        self.assertEqual(urlsplit(get.call_args_list[2].args[0].full_url).path, "/3/tv/123/images")

    @patch("find_backdrop.urlopen")
    def test_anime_and_other_search_both_media_types(self, get):
        for show_type in ("anime", "other"):
            get.reset_mock()
            get.side_effect = [response({"results": []}), response({"results": [MOVIE]}),
                               response(IMAGES), response(image())]
            self.fetch(show_type=show_type)
            self.assertEqual([urlsplit(call.args[0].full_url).path for call in get.call_args_list[:2]],
                             ["/3/search/tv", "/3/search/movie"])

    @patch("find_backdrop.time.sleep")
    @patch("find_backdrop.urlopen")
    def test_auth_failure_is_not_retried_and_never_exposes_key(self, get, sleep):
        get.side_effect = HTTPError("https://example.test/?api_key=SECRET", 401, "bad key", {}, None)
        with self.assertRaisesRegex(backdrop.BackdropError, r"TMDB returned HTTP 401\.$") as error:
            self.fetch()
        self.assertNotIn("SECRET", str(error.exception))
        self.assertEqual(get.call_count, 1)
        sleep.assert_not_called()

    @patch("find_backdrop.time.sleep")
    @patch("find_backdrop.urlopen")
    def test_transient_errors_are_retried_once(self, get, sleep):
        get.side_effect = [
            HTTPError("", 429, "", {}, None),
            response({"results": [MOVIE]}), response(IMAGES), response(image()),
        ]
        self.fetch()
        sleep.assert_called_once_with(0.5)
        get.side_effect = URLError("secret URL")
        with self.assertRaisesRegex(backdrop.BackdropError, "could not be reached"):
            self.fetch("Different Title")

    @patch("find_backdrop.time.sleep")
    @patch("find_backdrop.urlopen")
    def test_retry_after_numeric_and_http_date_delays_are_not_capped(self, get, sleep):
        now = 1700000000
        for header in ("8", formatdate(now + 8, usegmt=True)):
            with self.subTest(header=header):
                get.reset_mock()
                sleep.reset_mock()
                get.side_effect = [HTTPError("", 429, "", {"Retry-After": header}, None), response(b"ok")]
                with patch("find_backdrop.time.time", return_value=now):
                    self.assertEqual(
                        backdrop._read(backdrop.Request("https://example.test"), 100, provider="TMDB"),
                        b"ok",
                    )
                self.assertEqual(get.call_count, 2)
                sleep.assert_called_once_with(8)

    @patch("find_backdrop.time.sleep")
    @patch("find_backdrop.urlopen")
    def test_retry_after_exceeding_the_budget_never_sleeps_or_retries_early(self, get, sleep):
        now = 1700000000
        for header, budget in (
            ("60", 50), (formatdate(now + 60, usegmt=True), 50),
            ("1.5", 1), (None, 0.25),
        ):
            with self.subTest(header=header, budget=budget):
                get.reset_mock()
                sleep.reset_mock()
                headers = {} if header is None else {"Retry-After": header}
                get.side_effect = [HTTPError("", 429, "", headers, None), response(b"ok")]
                with patch("find_backdrop.time.time", return_value=now), \
                        patch("find_backdrop.time.monotonic", return_value=0):
                    with backdrop._lookup_deadline(budget):
                        with self.assertRaisesRegex(backdrop.BackdropError, "timed out"):
                            backdrop._read(backdrop.Request("https://example.test"), 100, provider="TMDB")
                self.assertEqual(get.call_count, 1)
                sleep.assert_not_called()

    @patch("find_backdrop.urlopen")
    def test_invalid_json_oversized_and_invalid_search_response(self, get):
        for data in (b"not JSON", b"x" * (backdrop.MAX_JSON_BYTES + 1), {"results": "bad"},
                     {"results": [], "total_pages": "bad"}):
            get.return_value = response(data)
            with self.assertRaises(backdrop.BackdropError):
                self.fetch()

    @patch("find_backdrop.urlopen")
    def test_only_landscape_paths_from_trusted_cdn_are_used(self, get):
        invalid = {"backdrops": [
            {"file_path": "https://example.test/image.jpg", "width": 1920, "height": 1080},
            {"file_path": "/../secret.jpg", "width": 1920, "height": 1080},
            {"file_path": "/portrait.jpg", "width": 500, "height": 750},
        ]}
        get.side_effect = [response({"results": [MOVIE]}), response(invalid),
                           response({**MOVIE, "backdrop_path": None})]
        with self.assertRaisesRegex(backdrop.BackdropError, "no suitable landscape"):
            self.fetch()
        self.assertEqual(get.call_count, 3)

    @patch("find_backdrop.urlopen")
    def test_bad_first_image_tries_another_without_caching_garbage(self, get):
        images = {"backdrops": [
            IMAGES["backdrops"][0],
            {"file_path": "/second.jpg", "width": 1280, "height": 720},
        ]}
        get.side_effect = [response({"results": [MOVIE]}), response(images),
                           response(b"<html>Blocked</html>"), response(image())]
        path = self.fetch()
        with Image.open(path) as result:
            result.load()
        self.assertEqual(get.call_count, 4)

    def test_manual_image_is_validated_and_cached_without_credentials(self):
        with patch("find_backdrop.load_credential", side_effect=AssertionError):
            path = backdrop.manual_backdrop(image(format="PNG"), self.directory)
        self.assertTrue(path.exists())
        for data in (b"not an image", image(360, 640), image(200, 100),
                     b"x" * (backdrop.MAX_IMAGE_BYTES + 1)):
            with self.assertRaises(backdrop.BackdropError):
                backdrop.manual_backdrop(data, self.directory)

    def test_bad_year_fails_before_credential_lookup(self):
        with patch("find_backdrop.load_credential") as credential:
            with self.assertRaisesRegex(backdrop.BackdropError, "four-digit"):
                self.fetch(year="bad")
        credential.assert_not_called()

    @patch("find_backdrop.subprocess.run")
    def test_pass_supports_read_token_or_v3_key_without_prompting(self, run):
        with patch.dict(os.environ, {}, clear=True):
            for value, bearer in (("jwt-token", True), ("a" * 32, False)):
                run.return_value = subprocess.CompletedProcess([], 0, value + "\nnotes", "")
                self.assertEqual(backdrop.load_credential(), backdrop.Credential(value, bearer))
        self.assertEqual(run.call_args.args[0], ["pass", "show", "--", "api/tmdb"])
        self.assertIn("--pinentry-mode error", run.call_args.kwargs["env"]["PASSWORD_STORE_GPG_OPTS"])

    @patch("find_backdrop.subprocess.run")
    def test_credentials_env_file_and_pass_failures_are_safe(self, run):
        self.assertEqual(backdrop.load_credential(), backdrop.Credential("test-token"))
        run.assert_not_called()
        file = self.directory / "credential"
        file.write_text("api-key")
        with patch.dict(os.environ, {"TMDB_API_KEY_FILE": str(file)}, clear=True):
            self.assertEqual(backdrop.load_credential(), backdrop.Credential("api-key", False))
        with patch.dict(os.environ, {}, clear=True):
            run.return_value = subprocess.CompletedProcess([], 1, "", "SECRET")
            with self.assertRaisesRegex(backdrop.BackdropError, "unavailable") as error:
                backdrop.load_credential()
            self.assertNotIn("SECRET", str(error.exception))

    @patch("find_backdrop.urlopen")
    def test_runner_2026_uses_known_imdb_identity_instead_of_ambiguous_search(self, get):
        runner = {"id": 1377237, "title": "Runner", "release_date": "2026-09-07"}
        get.side_effect = [
            response({"movie_results": [runner], "tv_results": []}),
            response(IMAGES), response(image()),
        ]
        self.fetch("Runner", "2026", imdb_id="tt31349844")
        request = get.call_args_list[0].args[0]
        self.assertEqual(urlsplit(request.full_url).path, "/3/find/tt31349844")
        self.assertEqual(parse_qs(urlsplit(request.full_url).query), {"external_source": ["imdb_id"]})
        self.assertEqual(urlsplit(get.call_args_list[1].args[0].full_url).path, "/3/movie/1377237/images")
        self.assertFalse(any("/search/" in call.args[0].full_url for call in get.call_args_list))

    @patch("find_backdrop.urlopen")
    def test_imdb_match_bypasses_title_and_year_but_respects_type(self, get):
        get.side_effect = [
            response({
                "movie_results": [{
                    **MOVIE,
                    "title": "Provider Canonical Title",
                    "release_date": "2004-01-01",
                }],
                "tv_results": [],
            }),
            response(IMAGES),
            response(image()),
        ]
        self.fetch("User Supplied Alias Season 1", "2003", imdb_id="tt0327137")

        get.reset_mock(side_effect=True)
        get.return_value = response({"movie_results": [], "tv_results": [MOVIE]})
        with self.assertRaisesRegex(backdrop.BackdropError, "selected media type"):
            self.fetch(imdb_id="tt0327137")

    @patch("find_backdrop.urlopen")
    def test_imdb_mapping_accepts_cyberpunk_season_label(self, get):
        series = {
            "id": 105248,
            "name": "Cyberpunk: Edgerunners",
            "first_air_date": "2022-09-13",
        }
        get.side_effect = [
            response({"movie_results": [], "tv_results": [series]}),
            response(IMAGES),
            response(image()),
        ]

        self.fetch(
            "Cyberpunk Edgerunners Season 1", None, "tv",
            imdb_id="tt12590266")

        self.assertEqual(
            urlsplit(get.call_args_list[1].args[0].full_url).path,
            "/3/tv/105248/images",
        )

    @patch("find_backdrop.urlopen")
    def test_imdb_mapping_missing_or_multiple_does_not_fall_back_to_guessed_title(self, get):
        for results in ([], [MOVIE, {**MOVIE, "id": 999}]):
            get.reset_mock()
            get.return_value = response({"movie_results": results})
            with self.assertRaisesRegex(backdrop.BackdropError, "could not uniquely match"):
                self.fetch(imdb_id="tt0327137")
            self.assertEqual(get.call_count, 1)

    @patch("find_backdrop.urlopen")
    def test_tv_imdb_mapping_accepts_parent_series_for_episode(self, get):
        series = {"id": 123, "name": "My Series", "first_air_date": "2020-01-01"}
        get.side_effect = [
            response({"movie_results": [], "tv_results": [series]}),
            response(IMAGES), response(image()),
        ]
        self.fetch("My Series episode 10", "2020", "tv", imdb_id="tt1234567")
        self.assertEqual(urlsplit(get.call_args_list[1].args[0].full_url).path, "/3/tv/123/images")

    @patch("find_backdrop.urlopen")
    def test_cache_separates_different_imdb_ids_for_same_title_year(self, get):
        get.side_effect = [
            response({"movie_results": [MOVIE]}), response(IMAGES), response(image()),
            response({"movie_results": [{**MOVIE, "id": 999}]}), response(IMAGES), response(image()),
        ]
        first = self.fetch(imdb_id="tt0327137")
        second = self.fetch(imdb_id="tt1234567")
        self.assertNotEqual(first, second)
        self.assertEqual(get.call_count, 6)

    @patch("find_backdrop.urlopen")
    def test_wrong_inferred_identity_is_rejected_before_artwork_or_cache(self, get):
        for changed in (
            {"title": "Wrong Film"}, {"release_date": "2004-01-01"},
            {"release_date": "2003garbage"},
        ):
            with self.subTest(changed=changed):
                get.reset_mock()
                get.return_value = response({"movie_results": [{**MOVIE, **changed}]})
                with self.assertRaisesRegex(backdrop.BackdropError, "inferred IMDb identity"):
                    self.fetch(imdb_id="tt0327137", imdb_id_explicit=False)
                self.assertEqual(get.call_count, 1)
                self.assertFalse((self.directory / "backdrops").exists())

    @patch("find_backdrop.urlopen")
    def test_inferred_id_cannot_reuse_an_authoritative_alias_cache(self, get):
        get.side_effect = [
            response({"movie_results": [MOVIE]}), response(IMAGES), response(image()),
        ]
        self.fetch("The Dark Knight", "2008", imdb_id="tt0327137")
        get.reset_mock(side_effect=True)
        get.return_value = response({"movie_results": [MOVIE]})
        with self.assertRaisesRegex(backdrop.BackdropError, "inferred IMDb identity"):
            self.fetch("The Dark Knight", "2008", imdb_id="tt0327137", imdb_id_explicit=False)
        self.assertEqual(get.call_count, 1)

    @patch("find_backdrop.urlopen")
    def test_verified_short_title_alias_is_accepted_for_inferred_identity(self, get):
        gremlins = {"id": 928, "title": "Gremlins 2: The New Batch", "release_date": "1990-06-15"}
        get.side_effect = [
            response({"movie_results": [gremlins]}), response(IMAGES), response(image()),
        ]
        self.fetch("Gremlins 2", "1990", imdb_id="tt0099700", imdb_id_explicit=False)
        get.reset_mock(side_effect=True)
        get.return_value = response({"movie_results": [gremlins]})
        with self.assertRaisesRegex(backdrop.BackdropError, "inferred IMDb identity"):
            self.fetch("Gremlins", "1990", imdb_id="tt0099700", imdb_id_explicit=False)

    @patch("find_backdrop.urlopen")
    def test_short_title_alias_search_never_accepts_an_arbitrary_prefix(self, get):
        gremlins = {"id": 928, "title": "Gremlins 2: The New Batch", "release_date": "1990-06-15"}
        get.side_effect = [
            response({"results": [gremlins]}), response({"results": [gremlins]}),
            response(IMAGES), response(image()),
        ]
        self.fetch("Gremlins 2", "1990")
        queries = [parse_qs(urlsplit(c.args[0].full_url).query)["query"][0]
                   for c in get.call_args_list[:2]]
        self.assertEqual(queries, ["Gremlins 2", "Gremlins 2: The New Batch"])

    @patch("find_backdrop.urlopen")
    def test_empty_image_listing_can_use_the_same_canonical_records_backdrop(self, get):
        get.side_effect = [
            response({"results": [MOVIE]}), response({"backdrops": []}),
            response({**MOVIE, "backdrop_path": "/landscape.jpg"}), response(image()),
        ]
        self.assertTrue(self.fetch().exists())
        self.assertEqual(urlsplit(get.call_args_list[2].args[0].full_url).path, "/3/movie/13156")

    @patch("find_backdrop.urlopen")
    def test_fallback_cover_from_another_record_is_rejected(self, get):
        get.side_effect = [
            response({"results": [MOVIE]}), response({"backdrops": []}),
            response({**MOVIE, "id": 999, "backdrop_path": "/landscape.jpg"}),
        ]
        with self.assertRaisesRegex(backdrop.BackdropError, "no suitable landscape"):
            self.fetch()
        self.assertEqual(get.call_count, 3)

    @patch("find_backdrop.urlopen")
    def test_a_fourth_available_image_is_not_lost_after_three_bad_candidates(self, get):
        images = {"backdrops": [
            {"file_path": f"/image{n}.jpg", "width": 1280, "height": 720}
            for n in range(4)
        ]}
        get.side_effect = [
            response({"results": [MOVIE]}), response(images),
            response(b"bad"), response(b"bad"), response(b"bad"), response(image()),
        ]
        self.assertTrue(self.fetch().exists())
        self.assertEqual(get.call_count, 6)

    @patch("find_backdrop.urlopen")
    def test_legacy_valid_image_without_identity_provenance_is_not_trusted(self, get):
        identity = ["secondhand lions", "2003", "movie"]
        old = self.directory / "backdrops" / f"tmdb-{hashlib.sha256(json.dumps(identity).encode()).hexdigest()}.jpg"
        old.parent.mkdir()
        old.write_bytes(image(color="red"))
        get.side_effect = [response({"results": [MOVIE]}), response(IMAGES), response(image())]
        fresh = self.fetch()
        self.assertNotEqual(fresh, old)
        self.assertEqual(get.call_count, 3)

    @patch("find_backdrop.urlopen")
    def test_tampered_or_expired_cache_metadata_forces_identity_resolution(self, get):
        for change in ({"identity": ["wrong"]}, {"created": 0},
                       {"created": float("nan")}, {"sha256": "wrong"}):
            with self.subTest(change=change):
                for manifest in (self.directory / "backdrops").glob("*.json"):
                    manifest.unlink()
                get.reset_mock()
                get.side_effect = [response({"results": [MOVIE]}), response(IMAGES), response(image())] * 2
                self.fetch()
                manifest = next((self.directory / "backdrops").glob("*.json"))
                data = json.loads(manifest.read_text())
                data.update(change)
                manifest.write_text(json.dumps(data))
                with patch("sys.stderr", new_callable=io.StringIO):
                    self.fetch()
                self.assertEqual(get.call_count, 6)

    def test_concurrent_cache_writers_return_immutable_images_with_a_consistent_pointer(self):
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        colors = iter(("red", "blue"))

        def download(*args, **kwargs):
            with lock:
                color = next(colors)
            barrier.wait(timeout=5)
            return image(color=color)

        with patch("find_backdrop.find_title", return_value=("movie", MOVIE["id"])), \
                patch("find_backdrop._backdrop_paths", return_value=["/landscape.jpg"]), \
                patch("find_backdrop._read", side_effect=download):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: self.fetch(), range(2)))
        self.assertNotEqual(results[0], results[1])
        pixels = []
        for path in results:
            with Image.open(path) as decoded:
                decoded.load()
                pixels.append(decoded.getpixel((0, 0)))
        self.assertNotEqual(pixels[0], pixels[1])
        with patch("find_backdrop.load_credential", side_effect=AssertionError("Cache miss")):
            self.assertIn(self.fetch(), results)

    def test_atomic_cache_write_failure_leaves_no_partial_file(self):
        destination = self.directory / "backdrops" / "manual.jpg"
        with patch("pathlib.Path.replace", side_effect=OSError("private filename")):
            with self.assertRaisesRegex(backdrop.BackdropError, "cache could not be written"):
                backdrop.save_image(image(), destination)
        self.assertEqual(list(destination.parent.iterdir()), [])

    @patch("find_backdrop._request")
    def test_repair_helpers_reject_malformed_provider_records_safely(self, request):
        request.return_value = {"results": [None]}
        with self.assertRaisesRegex(backdrop.BackdropError, "invalid repair candidate"):
            backdrop.repair_candidates("A movie", None, "movie")
        request.return_value = {"results": {}}
        with self.assertRaisesRegex(backdrop.BackdropError, "invalid repair candidates"):
            backdrop.repair_candidates("A movie", None, "movie")
        base = {**MOVIE, "credits": {"cast": [], "crew": []}, "external_ids": {"imdb_id": "tt0327137"}}
        for changed in (
            {"credits": None}, {"credits": {"cast": None}}, {"external_ids": None},
            {"external_ids": {"imdb_id": "../wrong"}}, {"title": None},
            {"release_date": 2003}, {"runtime": True},
        ):
            with self.subTest(changed=changed):
                request.return_value = {**base, **changed}
                with self.assertRaises(backdrop.BackdropError):
                    backdrop.repair_details("movie", MOVIE["id"])

    @patch("find_backdrop._request")
    def test_repair_imdb_find_is_queried_once_for_both_media_types(self, request):
        request.return_value = {"movie_results": [MOVIE], "tv_results": []}
        choices = backdrop.repair_candidates("tt0327137", None, "other")
        self.assertEqual(len(choices), 1)
        request.assert_called_once()

    @patch("find_backdrop.load_credential")
    def test_invalid_imdb_id_fails_before_credentials_or_network(self, credential):
        with self.assertRaisesRegex(backdrop.BackdropError, "IMDb identifier"):
            self.fetch(imdb_id="../secrets")
        credential.assert_not_called()

    @patch("find_backdrop.urlopen")
    def test_fallback_search_finds_exact_match_on_second_page(self, get):
        get.side_effect = [
            response({"total_pages": 2, "results": [{**MOVIE, "title": "Wrong Title"}]}),
            response({"results": [MOVIE]}), response(IMAGES), response(image()),
        ]
        self.fetch()
        params = parse_qs(urlsplit(get.call_args_list[1].args[0].full_url).query)
        self.assertEqual(params["page"], ["2"])

    @patch("find_backdrop.urlopen")
    def test_fallback_search_detects_ambiguity_across_pages(self, get):
        get.side_effect = [
            response({"total_pages": 2, "results": [MOVIE]}),
            response({"results": [{**MOVIE, "id": 999}]}),
        ]
        with self.assertRaisesRegex(backdrop.BackdropError, "Multiple"):
            self.fetch()
        self.assertEqual(get.call_count, 2)

    @patch("find_backdrop.urlopen")
    def test_excessive_fallback_pagination_is_bounded(self, get):
        get.return_value = response({"total_pages": backdrop.MAX_SEARCH_PAGES + 1, "results": [MOVIE]})
        with self.assertRaisesRegex(backdrop.BackdropError, "too many candidates"):
            self.fetch()
        self.assertEqual(get.call_count, 1)

    @patch("find_backdrop.urlopen")
    def test_repeated_or_changed_search_pages_cannot_hide_ambiguity(self, get):
        first = {"total_pages": 2, "page": 1, "results": [MOVIE]}
        for second in (
            {"total_pages": 2, "page": 2, "results": [MOVIE]},
            {"total_pages": 3, "page": 2, "results": []},
            {"total_pages": 2, "page": 1, "results": [{**MOVIE, "id": 999}]},
        ):
            with self.subTest(second=second):
                get.reset_mock()
                get.side_effect = [response(first), response(second)]
                with self.assertRaises(backdrop.BackdropError):
                    self.fetch()
                self.assertEqual(get.call_count, 2)

    @patch("find_backdrop.load_credential")
    def test_invalid_media_type_or_non_ascii_year_never_reads_credentials(self, credential):
        for args in ({"show_type": "series"}, {"year": "２００３"}, {"year": 2003}, {"imdb_id": ""}):
            with self.subTest(args=args):
                with self.assertRaises(backdrop.BackdropError):
                    self.fetch(**args)
        for kind, identifier in (("../movie", 1), ("movie", True), ("tv", -1)):
            with self.assertRaises(backdrop.BackdropError):
                backdrop.repair_details(kind, identifier)
        credential.assert_not_called()

    @patch("find_backdrop.fetch_backdrop", return_value=Path("verified.jpg"))
    def test_cli_preserves_the_inferred_identity_trust_boundary(self, fetch):
        with patch("sys.argv", ["find_backdrop.py", "Gremlins 2", "1990",
                                "--inferred-imdb-id", "tt0099700"]), \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(backdrop.main(), 0)
        self.assertEqual(fetch.call_args.kwargs["imdb_id"], "tt0099700")
        self.assertFalse(fetch.call_args.kwargs["imdb_id_explicit"])

    @patch("find_backdrop.fetch_backdrop", side_effect=backdrop.BackdropIdentityError("Wrong inferred film."))
    def test_cli_reports_identity_failure_separately_from_missing_artwork(self, fetch):
        with patch("sys.argv", ["find_backdrop.py", "The Dark Knight", "2008",
                                "--inferred-imdb-id", "tt0327137"]), \
                patch("sys.stdout", new_callable=io.StringIO) as output, \
                patch("sys.stderr", new_callable=io.StringIO) as error:
            self.assertEqual(backdrop.main(), 1)
        self.assertEqual(output.getvalue(), "NONE\n")
        self.assertIn("ERROR_CODE: inferred_identity_mismatch\n", error.getvalue())
        self.assertIn("ERROR: Wrong inferred film.\n", error.getvalue())

    @patch("find_backdrop.fetch_backdrop", side_effect=backdrop.BackdropError("Multiple matches."))
    def test_cli_exposes_a_safe_error_marker(self, fetch):
        with patch("sys.argv", ["find_backdrop.py", "Runner", "2026", "--imdb-id", "tt31349844"]), \
                patch("sys.stderr", new_callable=io.StringIO) as error, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(backdrop.main(), 1)
        self.assertEqual(error.getvalue(), "ERROR: Multiple matches.\n")
        self.assertEqual(output.getvalue(), "NONE\n")
        self.assertEqual(fetch.call_args.kwargs["imdb_id"], "tt31349844")


if __name__ == "__main__":
    unittest.main()
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
import hashlib

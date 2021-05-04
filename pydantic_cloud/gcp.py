import io
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Union, Mapping

from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import GoogleAuthError
from google.cloud import secretmanager
from pydantic import BaseSettings
from pydantic.env_settings import env_file_sentinel, read_env_file, SettingsError
from pydantic.utils import deep_update

logger = logging.getLogger(__name__)


def get_google_cloud_secret(key: str, encoding=None) -> Optional[str]:
    """
    Args:
        key: Resource name of secret to be fetched from Secret Manager
        encoding: Value for kwarg 'encoding' passed when
            decoding byte response from secret manager
    """

    try:
        client = secretmanager.SecretManagerServiceClient()
        response = client.access_secret_version(name=key)

        if encoding is not None:
            return response.payload.data.decode(encoding=encoding)
        else:
            return response.payload.data.decode()
    except (GoogleAuthError, GoogleAPIError) as e:
        logger.warning("Could not fetch (%s) from GCS: %s", key, e)


class CloudConfig(BaseSettings.Config):
    cloud_env_file = None
    cloud_env_file_encoding = None


def read_cloud_env_file(
        cloud_path: str,
        encoding: str = None,
        case_sensitive: bool = False
) -> Dict[str, Optional[str]]:
    try:
        from dotenv import dotenv_values
    except ImportError as e:
        raise ImportError('python-dotenv is not installed, run `pip install pydantic[dotenv]`') from e

    content = get_google_cloud_secret(cloud_path, encoding=encoding)
    stream = io.StringIO(content)

    file_vars: Dict[str, Optional[str]] = dotenv_values(stream=stream, encoding=encoding or 'utf8')
    if not case_sensitive:
        return {k.lower(): v for k, v in file_vars.items()}
    else:
        return file_vars


class GoogleCloudSecretSettings(BaseSettings):
    """
    Fetch setting value from Google Cloud Secret Manager.
    Local environment variables, env file, manually passed kwargs still take
    preference over values in GCS.
    Will fail silently if secret cannot be fetched for any reason

    Usage:
        For any key that must be fetched from Secret Manager, explicitly declare
        a Field() with the `cloud_key` argument, whose value is the env
        variable that contains the resource name of that key.

        .e.g

        class SampleSettings(GoogleCloudSecretSettings):
            MY_KEY: str = Field(cloud_key="MY_GOOG_KEY")

        The above program may then be run as:
            MY_GOOG_KEY=projects/<id>/secrets/<name>/versions/latest python app.py

        This will fetch the payload for secret "projects/<id>/secrets/<name>/versions/latest"
        from Google Cloud Secret Manager and use it as the value for "MY_KEY"
    """

    def _build_cloud_environ(self) -> Dict[str, Optional[str]]:
        if self.__config__.case_sensitive:
            env_vars: Mapping[str, Optional[str]] = os.environ
        else:
            env_vars = {k.lower(): v for k, v in os.environ.items()}

        cloud_env_file, cloud_env_file_encoding = (
            self.__config__.cloud_env_file, self.__config__.cloud_env_file_encoding
        )

        if cloud_env_file is not None:
            try:
                env_vars = {
                    **read_cloud_env_file(env_vars[cloud_env_file.lower()], encoding=cloud_env_file_encoding),
                    **env_vars,
                }
            except KeyError:
                logger.warning("%s not found in env", cloud_env_file)

        d: Dict[str, Optional[str]] = {}
        for field in self.__fields__.values():
            env_val: Optional[str] = None
            for env_name in field.field_info.extra['env_names']:
                env_val = env_vars.get(env_name)
                if env_val is not None:
                    break

            if env_val is None:
                continue

            if field.is_complex():
                try:
                    env_val = self.__config__.json_loads(env_val)  # type: ignore
                except ValueError as e:
                    raise SettingsError(f'error parsing JSON for "{env_name}"') from e
            d[field.alias] = env_val
        return d

    def _build_gcs_values(
            self, _env_file: Union[Path, str, None] = None, _env_file_encoding: Optional[str] = None
    ) -> Dict[str, Optional[str]]:
        if self.__config__.case_sensitive:
            env_vars: Mapping[str, Optional[str]] = os.environ
        else:
            env_vars = {k.lower(): v for k, v in os.environ.items()}

        env_file = _env_file if _env_file != env_file_sentinel else self.__config__.env_file
        env_file_encoding = _env_file_encoding if _env_file_encoding is not None else self.__config__.env_file_encoding
        if env_file is not None:
            env_path = Path(env_file).expanduser()
            if env_path.is_file():
                env_vars = {
                    **read_env_file(
                        env_path, encoding=env_file_encoding, case_sensitive=self.__config__.case_sensitive
                    ),
                    **env_vars,
                }

        env = dict()
        for field in self.__fields__.values():
            cloud_key = field.field_info.extra.get("cloud_key")
            if not isinstance(cloud_key, str):
                continue

            cloud_key = cloud_key.lower() if not self.__config__.case_sensitive else cloud_key
            if not env_vars.get(cloud_key):
                continue

            env[field.alias] = get_google_cloud_secret(env_vars[cloud_key])
        return env

    def _build_values(
            self,
            init_kwargs: Dict[str, Any],
            _env_file: Union[Path, str, None] = None,
            _env_file_encoding: Optional[str] = None,
            _secrets_dir: Union[Path, str, None] = None,
    ) -> Dict[str, Any]:
        return deep_update(
            self._build_cloud_environ(),
            self._build_gcs_values(_env_file, _env_file_encoding),
            self._build_secrets_files(_secrets_dir),
            self._build_environ(_env_file, _env_file_encoding),
            init_kwargs
        )

    __config__ = CloudConfig

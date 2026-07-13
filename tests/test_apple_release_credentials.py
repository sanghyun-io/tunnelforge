import pytest

from scripts.apple_release_credentials import determine_apple_release_mode


REQUIRED_CREDENTIALS = {
    "APPLE_CODESIGN_CERTIFICATE_P12_BASE64": "certificate",
    "APPLE_CODESIGN_CERTIFICATE_PASSWORD": "password",
    "APPLE_ID": "account@example.com",
    "APPLE_TEAM_ID": "TEAMID",
    "APPLE_APP_SPECIFIC_PASSWORD": "app-password",
}

OPTIONAL_CREDENTIALS = {
    "APPLE_CODESIGN_IDENTITY": "Developer ID Application: Example",
    "APPLE_CODESIGN_KEYCHAIN_PASSWORD": "keychain-password",
}


def test_no_apple_credentials_selects_unsigned_release():
    assert determine_apple_release_mode({}) == "unsigned"


@pytest.mark.parametrize(
    "name",
    [*REQUIRED_CREDENTIALS, *OPTIONAL_CREDENTIALS],
)
def test_any_single_apple_credential_fails_closed(name):
    values = {**REQUIRED_CREDENTIALS, **OPTIONAL_CREDENTIALS}

    with pytest.raises(ValueError, match="incomplete Apple release credentials"):
        determine_apple_release_mode({name: values[name]})


def test_complete_required_credentials_select_signed_release():
    assert determine_apple_release_mode(REQUIRED_CREDENTIALS) == "signed"


def test_complete_credentials_with_optional_values_select_signed_release():
    values = {**REQUIRED_CREDENTIALS, **OPTIONAL_CREDENTIALS}

    assert determine_apple_release_mode(values) == "signed"

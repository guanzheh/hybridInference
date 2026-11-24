"""Who a suspension applies to, as SQL."""

from serving.storage import organizations as orgs


def test_a_suspension_asks_the_status_and_the_account():
    assert orgs.suspended_for("o", "ou", "$3") == (
        "(o.status = 'suspended' AND (ou.role IS DISTINCT FROM 'admin' "
        "AND NOT COALESCE(lower(ou.email) = ANY($3::text[]), FALSE)))"
    )


def test_single_sign_on_only_and_a_suspension_leave_the_same_accounts_out():
    """One definition of a platform administrator for the two rules.

    The password and the phone sign-in refuse an account that signs in through
    single sign-on only, and read no status of its organization. That is
    complete only while a suspension applies to no other account.
    """
    assert orgs._SSO_ONLY_ACCOUNT.format(exempt=2) == orgs.not_platform_administrator("u", "$2")

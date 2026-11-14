"""A mid-pass claim change must not lend its epoch to an older decision."""
from unittest import mock

from mncs_env import projections, sessions
from test_projection_verification import NEED_MNCS, ProjectionFixture


@NEED_MNCS
class ClaimEpochTests(ProjectionFixture):
    def test_release_after_decision_does_not_cache_pending_under_released_claims(self):
        code, first = self.enter('mid-pass-claim-release')
        self.assertEqual(code, 0, first)
        self.acquire_foreign('target-doc', ['docs/out.generated.md'])
        (self.target_doc / 'docs/rfcs/0002.md').write_text('# RFC 0002 changed\n')
        self.commit_all(self.target_doc, 'advance blocked source')
        session = sessions.Session.open(state_dir=self.state,
                                        session_id=first['session_id'], backend='file')
        original = projections.claim_facts
        captures = 0

        def capture(session, repository, output):
            nonlocal captures
            observed = original(session, repository, output)
            if repository == 'target-doc':
                captures += 1
                # The projection sampled a foreign claim. Release before the
                # verifier's fresh admission so it can produce real PASS,
                # while this pass still plans against the older claim fact.
                self.release_foreign('target-doc')
            return observed

        try:
            with mock.patch.dict('os.environ', self.env):
                session.revalidate()
            with mock.patch.dict('os.environ', self.env), mock.patch.object(
                    projections, 'claim_facts', side_effect=capture):
                blocked = projections.ambient_pass(session)
            self.assertEqual(captures, 1)
            self.assertIn('target-doc:index', blocked['summary']['pending_ids'])
            self.assertTrue(session.snapshot['projection_epoch']['epoch'].startswith('uncached:'),
                            session.snapshot['projection_epoch'])
            with mock.patch.dict('os.environ', self.env):
                recovered = projections.ambient_pass(session)
            self.assertFalse(recovered['reused'], recovered)
            self.assertEqual(recovered['summary']['pending'], 0, recovered)
            self.assertGreaterEqual(recovered['summary']['reconciled'], 1, recovered)
        finally:
            session.close()

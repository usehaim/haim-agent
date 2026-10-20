These workflows only run once this directory is published as a repository of
its own. While the component lives inside the HAIM monorepo, GitHub reads only
the .github at the repository root - so the root workflow .github/agent-ci.yml
is what actually runs the tests, and this pair is here so that publishing the
directory needs no extra work.

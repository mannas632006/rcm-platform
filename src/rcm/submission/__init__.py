"""Outbound submission to clearinghouses (M3.7, SRS 6.3-6.4).

Everything that leaves the platform for a clearinghouse goes through this
package: the adapter interface (IR-08), the circuit breaker and router
(IR-09), and the transactional outbox (IR-06, IR-07). It sits above
`rcm.work` in the layer order because a clearinghouse outage is raised as a
work item for a person.

Only mock adapters exist. A real clearinghouse connection is a human
checkpoint (CLAUDE.md section 12), and the choice of secondary is
DECISION-06.
"""

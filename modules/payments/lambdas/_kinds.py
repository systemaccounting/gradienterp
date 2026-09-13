"""The failure kinds this module's lambdas raise, declared once (aws.Kind)."""

from aws import Kind

STRIPE_CALL_FAILED = Kind("stripe_call_failed", "stripe refused or failed the call", "dependency",
                          ids=("contact_id", "session_id", "invoice_id"))
CONTACT_READ_FAILED = Kind("contact_read_failed", "the contact could not be read", "dependency", ids=("contact_id",))
CONTACT_WRITE_FAILED = Kind("contact_write_failed", "the contact could not be written", "dependency", ids=("contact_id",))
INVOKE_FAILED = Kind("invoke_failed", "a downstream invoke errored", "dependency", ids=())
COLLECTION_REFUSED = Kind("collection_refused", "invoicing refused the collection", "data", ids=("invoice_id",))
